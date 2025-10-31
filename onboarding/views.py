# onboarding/views.py

import os
import json
import re
import requests
import pandas as pd
import threading
from dotenv import load_dotenv
from openai import OpenAI
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from .models import Candidate

# ==============================
# Env / Client
# ==============================
load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
print("🔑 OpenAI key loaded:", bool(client.api_key))

# Swap models here if needed
MAIN_MODEL = os.getenv("MAIN_MODEL", "gpt-4o")       # "gpt-4o" is faster than gpt-5
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "gpt-4o-mini")  # cheap classifier

with open("onboarding/data/inplace_onboarding.txt", "r", encoding="utf-8") as f:
    onboarding_data = f.read()

# ==============================
# Utilities (Lang, HTTP, Params)
# ==============================

def detect_language(text: str) -> str:
    """
    Improved language detection for EN/IT and other languages.
    Uses better heuristics and returns detected language or 'en' as fallback.
    """
    if not text:
        return "en"
    
    t = text.strip().lower()
    
    # Italian markers
    it_markers = [
        "ciao", "grazie", "buongiorno", "buonasera", "buonanotte", "salve",
        "nome", "cognome", "documento", "firma", "codice", "come", "cosa",
        "residenza", "comune", "registrati", "verifica", "email", "italiano",
        "esempio", "posso", "aiuto", "piacere", "scusa", "prego", "certo"
    ]
    
    # English markers
    en_markers = [
        "hello", "hi", "hey", "thanks", "thank", "good morning", "good evening",
        "good night", "name", "surname", "document", "signature", "code",
        "how", "what", "where", "register", "verify", "email", "english",
        "example", "can", "help", "please", "sorry", "sure", "yes", "no"
    ]
    
    # Calculate marker presence
    it_score = sum(1 for marker in it_markers if marker in t)
    en_score = sum(1 for marker in en_markers if marker in t)
    
    # Check for specific Italian patterns
    it_patterns = ["è", "è", "à", "é", "ù", "ò", "perché", "che", "del", "della", "gli", "le"]
    if any(pattern in t for pattern in it_patterns):
        it_score += 2
    
    # Determine language
    if it_score > en_score and it_score > 0:
        return "it"
    elif en_score > it_score and en_score > 0:
        return "en"
    elif it_score == en_score and it_score > 0:
        # If tie, check for characteristic characters
        if any(c in t for c in "àèéìòù"):
            return "it"
        return "en"
    
    # Fallback to English if no clear markers
    return "en"


def send_text_message(phone_number: str, body: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    print(f"📨 Text send -> {phone_number}: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()


def gpt_params_for_model(model_name: str, messages, timeout: int = 8):
    """
    GPT-4o for speed, supports temperature/top_p/penalties.
    """
    base = dict(model=model_name, timeout=timeout, messages=messages)
    # Enable anti-repetition + natural variety
    base.update(dict(temperature=0.7, top_p=1, frequency_penalty=0.7, presence_penalty=0.3))
    return base


# ==============================
# Lightweight memory in history
# ==============================

def get_state_objects(history):
    """
    Read prior 'state' and 'summary' items from history.
    Stored as: {"from":"state","text":"{json}"}, {"from":"summary","text":"..."}
    Returns (last_state_dict_or_None, last_summary_str_or_None)
    """
    last_state = None
    last_summary = None
    if not history:
        return None, None
    for m in history:
        if m.get("from") == "state":
            try:
                last_state = json.loads(m.get("text", "{}"))
            except Exception:
                pass
        elif m.get("from") == "summary":
            last_summary = m.get("text", "")
    return last_state, last_summary


def summarize_if_needed(candidate):
    """Occasional rolling summary to keep context coherent without long histories."""
    history = candidate.history or []
    if len(history) < 60:
        return
    # Find last summary index
    last_summary_idx = None
    for i, m in enumerate(history):
        if m.get("from") == "summary":
            last_summary_idx = i
    start = (last_summary_idx + 1) if last_summary_idx is not None else 0
    window = [m for m in history[start:] if m.get("from") in {"user", "bot", "admin"}][-40:]
    if not window:
        return
    transcript = "\n".join([f"{m['from']}: {m['text']}" for m in window])

    prompt = f"""
Summarize this conversation window into 4–7 bullet points (<=120 words), preserving decisions, user preferences, and current step. Keep {'Italian' if detect_language(transcript) == 'it' else 'English'}.

--- WINDOW ---
{transcript}
--- END ---
"""
    try:
        res = client.chat.completions.create(
            **gpt_params_for_model(CLASSIFIER_MODEL, [
                {"role": "system", "content": "You produce concise, faithful summaries."},
                {"role": "user", "content": prompt}
            ], timeout=12)
        )
        summary = res.choices[0].message.content.strip()
        candidate.history.append({"from": "summary", "text": summary})
        candidate.save()
    except Exception as e:
        print("⚠️ Summary failed:", e)


# ==============================
# Orchestrated GPT Responding
# ==============================

def build_dialogue_messages(candidate, user_msg: str, lang: str, is_first_inbound: bool):
    """
    Build messages for GPT:
    - Persona + rules (bilingual, human-like, no repetition)
    - Orchestrator JSON instruction
    - Optional memory summary + last state
    - Recent transcript
    - First-contact guidance (if first inbound)
    """
    history = candidate.history or []
    last_state, last_summary = get_state_objects(history)
    recent = [m for m in history if m.get("from") in {"user", "bot", "admin"}][-6:]

    base_style_it = """
Sei un assistente per l’onboarding InPlace.it, bilingue (Italiano/English).
Regole:
- Riconosci la lingua del messaggio corrente e rispondi in quella lingua. Se l’utente cambia lingua, cambia anche tu.
- Niente frasi robotiche o ripetitive; varia le formulazioni. Evita “Come posso aiutarti oggi?”.
- Risposte brevi (1–6 frasi), specifiche al contesto; non ripetere saluti.
- Ricorda quanto deciso prima e proponi un prossimo passo chiaro e coerente.
- Non chiedere le stesse info due volte se già fornite.
- Se l’utente chiede un umano, offri l’escalation. Non inventare dati.
"""
    base_style_en = """
You are an InPlace.it onboarding assistant, bilingual (English/Italian).
Rules:
- Detect the language of the CURRENT message and reply in that language. If the user switches languages mid-chat, you also switch.
- No robotic or repetitive phrasing; avoid “How can I assist you today?”. Vary wording.
- Keep replies short (1–6 sentences), context-specific; don’t repeat greetings.
- Remember prior decisions and always propose a clear, coherent next step.
- Don’t ask for the same info twice if already provided.
- If the user asks for a human, offer escalation. Do not fabricate facts.
"""

    # First-contact guidance (works even if first msg is a question)
    first_contact_it = """
Se questo è il PRIMO messaggio dell’utente:
- Se il messaggio è un semplice saluto o apertura (“ciao”, “buongiorno”, ecc.), rispondi con un benvenuto caldo e breve, spiega in 1 riga come puoi aiutare (onboarding InPlace: registrazione, documenti, firme, accessi) e chiedi gentilmente da dove vuole iniziare.
- Se il messaggio è già una domanda/azione (non un saluto), vai dritto al punto: rispondi e proponi il passo successivo senza introdurre formule generiche.
"""
    first_contact_en = """
If this is the user’s FIRST message:
- If it’s a simple greeting/opener (“hi”, “hello”, etc.), reply with a warm, brief welcome, explain in 1 line how you help (InPlace onboarding: registration, docs, signatures, access) and ask politely where they want to begin.
- If it’s already a question/action (not just a greeting), get straight to it: answer and propose the next step—no generic intros.
"""

    orchestrator = f"""
Output ONLY valid JSON with this schema:

{{
  "reply": "string - user-facing answer in {'Italian' if lang=='it' else 'English'}, concise, human-like",
  "intent": "string - inferred intent (greeting, registration_help, docs_help, signature_help, access_help, proceed_step, thanks, goodbye, other)",
  "next_step": "string - suggested next move (e.g., ask for doc X, confirm step Y)",
  "state_update": {{
      "step": "string|null - current onboarding step if applicable",
      "flags": {{"wants_human": false, "confused": false, "frustrated": false}},
      "notes": "string - short memory to keep context (<=200 chars)"
  }}
}}

Behavioral rules:
- Use current-message language; if the user switches languages, switch too automatically.
- Never claim you can help only in one language—you are bilingual.
- Do NOT restart the flow on “ok/thanks/hi”. Continue smoothly with a coherent next step.
- Avoid repetitive greetings or apologies.
"""

    system_prompt = (
        (base_style_it if lang == "it" else base_style_en)
        + "\n"
        + (first_contact_it if lang == "it" else first_contact_en)
        + "\n\nKnowledge base:\n"
        + onboarding_data
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": orchestrator}
    ]

    if last_summary:
        messages.append({"role": "system", "content": f"Conversation summary so far:\n{last_summary}"})
    if last_state:
        messages.append({"role": "system", "content": f"State memory:\n{json.dumps(last_state, ensure_ascii=False)}"})
    if recent:
        transcript = "\n".join([f"{m['from']}: {m['text']}" for m in recent])
        messages.append({"role": "system", "content": f"Recent transcript:\n{transcript}"})

    # Explicit first-contact flag helps the model choose tone without being generic
    if is_first_inbound:
        messages.append({"role": "system", "content": "FIRST_CONTACT: true"})
    else:
        messages.append({"role": "system", "content": "FIRST_CONTACT: false"})

    messages.append({"role": "user", "content": user_msg})
    return messages


def orchestrated_reply(candidate, incoming_msg: str):
    """
    One GPT call that returns a JSON with reply + state and saves state in history.
    Language is selected from the CURRENT message so we can switch mid-chat.
    """
    history = candidate.history or []
    # first user message if count of user entries == 1 after appending
    user_msgs = [m for m in history if m.get("from") == "user"]
    is_first_inbound = len(user_msgs) == 1
    lang = detect_language(incoming_msg)

    messages = build_dialogue_messages(candidate, incoming_msg, lang, is_first_inbound)

    try:
        res = client.chat.completions.create(
            **gpt_params_for_model(MAIN_MODEL, messages, timeout=8)
        )
        raw = res.choices[0].message.content.strip()
        print("🧠 Orchestrator RAW:", raw)

        # Clean markdown code blocks if present (GPT sometimes wraps JSON in ```json ... ```)
        cleaned = raw
        # More robust markdown code fence removal
        if "```" in cleaned:
            # Remove opening code fence (```json, ```, etc.) - handle with or without language identifier
            cleaned = re.sub(r'^```\w*\s*\n?', '', cleaned, flags=re.MULTILINE)
            # Remove closing code fence
            cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE)
            cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            print(f"⚠️ JSON parse error: {e}, using fallback")
            # If parsing fails, treat the entire response as the reply (don't send raw JSON)
            # Extract any text that looks like a natural language response
            if cleaned.startswith("{") and cleaned.endswith("}"):
                # It's JSON but malformed - try to extract reply manually
                try:
                    # Try to extract reply field from JSON string manually
                    # Look for "reply": "..." pattern, handling escaped quotes and multiline strings
                    # This regex handles: "reply": "text" or "reply":"text" with escaped quotes
                    # Also handles multiline strings with proper escaping
                    match = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
                    if match:
                        reply_text = match.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
                    else:
                        # Last resort: return a generic message instead of raw JSON
                        reply_text = "Ok." if lang == "en" else "Ok."
                    data = {"reply": reply_text, "state_update": None, "intent": "other", "next_step": ""}
                except Exception:
                    data = {"reply": "Ok." if lang == "en" else "Ok.", "state_update": None, "intent": "other", "next_step": ""}
            else:
                # Not JSON, use as-is
                data = {"reply": cleaned, "state_update": None, "intent": "other", "next_step": ""}

        # Extract reply field - ensure it's always a string, never raw JSON
        reply = data.get("reply")
        # Handle None, empty string, or other falsy values
        if reply is None or reply == "":
            reply = ""
        elif isinstance(reply, dict):
            # If reply is accidentally a dict/object, don't send it - use fallback
            print("⚠️ Reply field is a dict, using fallback")
            reply = ""
        elif isinstance(reply, list):
            # If reply is a list, don't send it - use fallback
            print("⚠️ Reply field is a list, using fallback")
            reply = ""
        elif not isinstance(reply, str):
            # Convert other types to string, but check if it looks like JSON first
            reply_str = str(reply)
            if reply_str.startswith("{") and reply_str.endswith("}"):
                print("⚠️ Reply field converted to string but looks like JSON, using fallback")
                reply = ""
            else:
                reply = reply_str
        
        reply = reply.strip() if reply else ""
        
        # Final safety check: if reply looks like JSON (starts with { and ends with }), don't send it
        # Also check for orchestrator JSON structure markers to catch the full response being sent
        is_orchestrator_json = (
            reply.startswith("{") and reply.endswith("}") and
            ("\"intent\"" in reply or "\"next_step\"" in reply or "\"state_update\"" in reply)
        )
        
        if reply.startswith("{") and reply.endswith("}"):
            try:
                # Try to parse it and extract meaningful text
                parsed_json = json.loads(reply)
                # If it's the full orchestrator response structure, extract the reply field again
                if "reply" in parsed_json and isinstance(parsed_json["reply"], str):
                    extracted_reply = parsed_json["reply"].strip()
                    # Make sure the extracted reply isn't also JSON
                    if extracted_reply and not (extracted_reply.startswith("{") and extracted_reply.endswith("}")):
                        reply = extracted_reply
                    else:
                        # Extracted reply is also JSON - use fallback
                        print("⚠️ Extracted reply is also JSON, using fallback")
                        reply = "Ok." if lang == "en" else "Ok."
                else:
                    # If no valid reply field, use a generic message
                    print("⚠️ No valid reply field in JSON structure, using fallback")
                    reply = "Ok." if lang == "en" else "Ok."
            except Exception:
                # If it's not valid JSON or can't extract, use fallback
                print("⚠️ Could not parse JSON in final safety check, using fallback")
                reply = "Ok." if lang == "en" else "Ok."
        
        if not reply:
            reply = "Ok." if lang == "en" else "Ok."

        # Persist state
        su = data.get("state_update")
        if su:
            try:
                candidate.history.append({"from": "state", "text": json.dumps(su, ensure_ascii=False)})
                candidate.save()
            except Exception as e:
                print("⚠️ Failed to save state:", e)

        return reply

    except Exception as e:
        print("[GPT ERROR]:", e)
        return "Sorry, something went wrong. Please try again later." if lang == "en" else "Spiacente, si è verificato un errore. Riprova più tardi."


# ==============================
# Meta Template (unchanged)
# ==============================

def send_onboarding_template(phone_number, name):
    print(f"🔔 Sending message to: {phone_number}")
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": {
            "name": "onboarding_named",
            "language": {"code": "it"},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {
                                "link": "https://instant-avatar.com/document/Privacy%20whatsapp.pdf",
                                "filename": "Informativa_InPlace.pdf"
                            }
                        }
                    ]
                },
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "parameter_name": "first_name",
                            "text": name
                        }
                    ]
                }
            ]
        }
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"📨 Meta response: {response.status_code} {response.text}")
    response.raise_for_status()
    return response.json()


# ==============================
# Escalation Helpers
# ==============================

def check_immediate_escalation(message: str) -> bool:
    """
    Fast keyword-based escalation check for explicit human requests.
    Uses phrase matching to avoid false positives.
    """
    msg_lower = message.lower()
    
    # Italian keywords - phrases only, not single words
    it_keywords = [
        "parlare con un operatore",
        "parlare con una persona",
        "contattare un umano",
        "assistenza umana",
        "voglio un operatore",
        "voglio parlare con",
        "posso parlare con una persona",
        "ho bisogno di parlare con un operatore"
    ]
    
    # English keywords - phrases only
    en_keywords = [
        "speak to a human",
        "talk to an operator",
        "contact a person",
        "human assistance",
        "i need a person",
        "real person",
        "talk to a person",
        "speak with an agent"
    ]
    
    # Check if message contains any escalation phrase
    for phrase in it_keywords + en_keywords:
        if phrase in msg_lower:
            return True
    
    return False


def run_background_escalation_check(candidate, incoming_msg: str):
    """
    Background escalation analysis using GPT.
    Runs after reply is sent to detect frustration without blocking.
    """
    try:
        if candidate.status == "escalated":
            return
        
        # Only check every 3rd message to reduce API calls
        user_messages = [m for m in candidate.history if m.get("from") == "user"]
        should_check_escalation = len(user_messages) % 3 == 0 or len(user_messages) == 1
        
        if not should_check_escalation:
            return
        
        chat_history = candidate.history[-5:] if candidate.history else []
        chat_history_text = "\n".join(
            [f"{m['from']}: {m['text']}" for m in chat_history] + [f"user: {incoming_msg}"]
        )

        classification_prompt = f"""
You are an escalation analyzer for a support chatbot.

Return JSON with:
- frustration_score (0-10)
- human_request_score (0-10)
- confusion_score (0-10)
- repeat_count (0-10)

Escalate only if scores are high; do not escalate for polite help/thanks.

--- CHAT START ---
{chat_history_text}
--- CHAT END ---
"""
        result = client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[{"role": "system", "content": classification_prompt}],
            timeout=5
        )

        response_text = result.choices[0].message.content
        scores = json.loads(response_text)
        
        f = scores.get("frustration_score", 0)
        h = scores.get("human_request_score", 0)
        c = scores.get("confusion_score", 0)
        r = scores.get("repeat_count", 0)

        if f >= 7 or h >= 8 or (c >= 8 and r >= 3):
            escalation_reason = f"Escalated (F:{f}, H:{h}, C:{c}, R:{r})"
            candidate.status = "escalated"
            candidate.escalation_reason = escalation_reason
            candidate.save()
            send_escalation_email(candidate)
            print(f"⛔ Background escalation: {escalation_reason}")
    except Exception as e:
        print(f"⚠️ Background escalation check failed: {e}")


# ==============================
# Webhook
# ==============================

def process_webhook_message(data: dict):
    """
    Background processing function for webhook messages.
    This runs in a separate thread to avoid blocking the response.
    """
    try:
        value = data['entry'][0]['changes'][0]['value']
        if 'messages' not in value:
            return
            
        incoming_msg = value['messages'][0]['text']['body']
        sender_id = value['messages'][0]['from']
        message_id = value['messages'][0].get('id')
        
        print(f"Processing message: {incoming_msg[:50]}")

        candidate, _ = Candidate.objects.get_or_create(
            phone_number=sender_id,
            defaults={'name': 'Unknown', 'surname': 'Unknown', 'processed_message_ids': []}
        )

        # Initialize fields if None
        if candidate.processed_message_ids is None:
            candidate.processed_message_ids = []
        if candidate.history is None:
            candidate.history = []
        
        # MESSAGE DEDUPLICATION - prevent loop
        if message_id and message_id in candidate.processed_message_ids:
            print(f"⚠️ Duplicate message {message_id} - skipping")
            return
        
        # Mark message as processed
        if message_id:
            candidate.processed_message_ids.append(message_id)
            # Keep only last 100 message IDs
            if len(candidate.processed_message_ids) > 100:
                candidate.processed_message_ids = candidate.processed_message_ids[-100:]
        
        candidate.history.append({"from": "user", "text": incoming_msg})
        candidate.save()

        # ===== TWO-TIER ESCALATION =====
        
        # Tier 1: Fast keyword check BEFORE reply (explicit human requests)
        if check_immediate_escalation(incoming_msg):
            candidate.status = "escalated"
            candidate.escalation_reason = "Immediate escalation (explicit request)"
            candidate.save()
            send_escalation_email(candidate)
            
            # Send handoff message
            lang = detect_language(incoming_msg)
            handoff_msg = "Ti metto in contatto con un operatore. A breve riceverai assistenza." if lang == "it" else "I'll connect you with an operator. You'll receive assistance shortly."
            send_text_message(sender_id, handoff_msg)
            print(f"⛔ Immediate escalation triggered")
            return
        
        # Skip if already escalated
        if candidate.status == 'escalated':
            print("⛔ Bot paused for this user (already escalated).")
            return

        # ===== Orchestrated normal reply =====
        reply = orchestrated_reply(candidate, incoming_msg)

        # Save + send
        candidate.history.append({"from": "bot", "text": reply})
        candidate.status = "replied"
        candidate.save()

        send_text_message(sender_id, reply)
        print("✅ Replied successfully")

        # ===== Tier 2: Background escalation analysis (after reply sent) =====
        # Spawn background thread for GPT-based frustration detection
        thread = threading.Thread(target=run_background_escalation_check, args=(candidate, incoming_msg))
        thread.daemon = True
        thread.start()
        
        # Run summarization in background too
        def background_summarize():
            try:
                summarize_if_needed(candidate)
            except Exception as e:
                print(f"⚠️ Background summary failed: {e}")
        
        summary_thread = threading.Thread(target=background_summarize)
        summary_thread.daemon = True
        summary_thread.start()

    except Exception as e:
        print("❌ Error in background processing:", e)


@csrf_exempt
def meta_webhook(request):
    """Webhook handler - returns 200 OK immediately and processes in background"""
    if request.method == 'GET':
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return HttpResponse(challenge)
        return HttpResponse("Verification failed", status=403)

    if request.method == 'POST':
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception as e:
            print("Error parsing JSON:", e)
            return HttpResponse(status=400)

        print("Incoming from Meta")
        
        # RETURN 200 OK IMMEDIATELY - process in background
        # Start background thread to process message
        thread = threading.Thread(target=process_webhook_message, args=(data,))
        thread.daemon = True
        thread.start()

        return JsonResponse({"status": "received"})


# ==============================
# Admin / Upload / Reports (unchanged)
# ==============================

@csrf_exempt
def upload_excel(request):
    if request.method == 'POST':
        file = request.FILES.get('file')
        if not file:
            return JsonResponse({'error': 'No file uploaded'}, status=400)

        try:
            df = pd.read_excel(file)
            added, skipped, failed = 0, 0, []

            for _, row in df.iterrows():
                phone = str(row.get('phone_number')).replace("+", "").replace(" ", "")
                if not phone or phone.lower() == 'nan':
                    failed.append(phone)
                    continue

                if Candidate.objects.filter(phone_number=phone).exists():
                    skipped += 1
                    continue

                Candidate.objects.create(
                    name=row.get('name', 'Unknown'),
                    surname=row.get('surname', 'Unknown'),
                    phone_number=phone,
                    status='sent'
                )

                try:
                    name = str(row.get('name', '')).strip()
                    if not name:
                        name = "Amico"
                    print(f"📤 Sending to {phone} with name: {name}")
                    send_onboarding_template(phone, name)
                    added += 1
                except Exception as e:
                    print(f"❌ Failed to send to {phone}: {e}")
                    failed.append(phone)

            return JsonResponse({'success': True, 'added': added, 'skipped': skipped, 'failed': failed})

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return render(request, 'admin_panel.html', {
        'candidates': Candidate.objects.all().order_by('-last_updated')[:200]
    })


@require_GET
def get_escalated(request):
    candidates = Candidate.objects.filter(status='escalated')
    data = [{'name': c.name, 'phone_number': c.phone_number} for c in candidates]
    return JsonResponse(data, safe=False)


@require_GET
def get_chat_history(request):
    phone = request.GET.get('phone')
    try:
        candidate = Candidate.objects.get(phone_number=phone)
        return JsonResponse({'history': candidate.history or []})
    except Candidate.DoesNotExist:
        return JsonResponse({'history': []})


@csrf_exempt
@require_POST
def send_admin_reply(request):
    data = json.loads(request.body)
    phone = data.get('phone_number')
    text = data.get('text')

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, json=payload, headers=headers)

    candidate = Candidate.objects.get(phone_number=phone)
    if candidate.history is None:
        candidate.history = []
    candidate.history.append({"from": "admin", "text": text})
    candidate.save()

    return JsonResponse({"sent": True})


@csrf_exempt
@require_POST
def resume_bot(request):
    data = json.loads(request.body)
    phone = data.get('phone_number')
    try:
        candidate = Candidate.objects.get(phone_number=phone)
        candidate.status = 'replied'
        candidate.escalation_reason = None

        # Trim chat history to remove old frustration context
        if candidate.history:
            candidate.history = candidate.history[-3:]  # keep only last 3 messages

        candidate.save()
        return JsonResponse({"resumed": True})
    except Candidate.DoesNotExist:
        return JsonResponse({"resumed": False})


@require_GET
def get_all_chats(request):
    candidates = Candidate.objects.exclude(history=None).order_by('-last_updated')
    data = []
    for c in candidates:
        last_msg = c.history[-1] if c.history else {}
        data.append({
            "name": c.name,
            "phone_number": c.phone_number,
            "status": c.status,
            "last_message": last_msg.get("text", ""),
            "last_sender": last_msg.get("from", ""),
            "last_updated": c.last_updated.strftime("%Y-%m-%d %H:%M")
        })
    return JsonResponse(data, safe=False)


from django.db.models import Count

@require_GET
def get_report_stats(request):
    candidates = Candidate.objects.all()
    total_users = candidates.count()
    total_messages = 0
    bot_messages = 0
    user_messages = 0
    admin_messages = 0
    conversation_lengths = []

    for c in candidates:
        history = c.history or []
        total_messages += len(history)
        conversation_lengths.append(len(history))
        for m in history:
            sender = m.get("from")
            if sender == "bot":
                bot_messages += 1
            elif sender == "user":
                user_messages += 1
            elif sender == "admin":
                admin_messages += 1

    average_length = round(sum(conversation_lengths) / total_users, 2) if total_users > 0 else 0

    sent = candidates.filter(status='sent').count()
    replied = candidates.filter(status='replied').count()
    escalated = candidates.filter(status='escalated').count()

    # Define "Completed Onboarding" as having at least 6 bot replies
    completed_onboarding = sum(
        1 for c in candidates if sum(1 for m in (c.history or []) if m.get("from") == "bot") >= 6
    )

    with_reason = sum(1 for c in candidates if c.status == 'escalated' and c.escalation_reason)

    return JsonResponse({
        "summary": {
            "total_users": total_users,
            "total_messages": total_messages,
            "average_conversation_length": average_length,
            "bot_messages": bot_messages,
            "user_messages": user_messages,
            "admin_messages": admin_messages,
        },
        "engagement_funnel": {
            "sent": sent,
            "replied": replied,
            "completed_onboarding": completed_onboarding,
            "escalated": escalated
        },
        "escalation_stats": {
            "total_escalated": escalated,
            "with_reason": with_reason
        }
    })


from django.core.mail import send_mail

def send_escalation_email(candidate):
    subject = f"[Escalation Alert] {candidate.name or 'Unknown'} ({candidate.phone_number})"
    message = f"""
⚠️ A user has been escalated!

Name: {candidate.name}
Phone: {candidate.phone_number}
Reason: {candidate.escalation_reason or 'N/A'}

Check the admin panel for full chat history.

— InPlace Onboarding Bot
"""
    try:
        send_mail(
            subject,
            message,
            os.getenv("EMAIL_HOST_USER"),
            [os.getenv("ADMIN_ALERT_EMAIL")],
            fail_silently=False,
        )
        print("✅ Email sent to admin.")
    except Exception as e:
        print("❌ Failed to send email:", e)
