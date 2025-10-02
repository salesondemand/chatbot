# onboarding/views.py

import os
import json
import requests
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI, APIConnectionError, APITimeoutError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from .models import Candidate

# Load environment variables
load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
print("🔑 OpenAI key loaded:", bool(client.api_key))

with open("onboarding/data/inplace_onboarding.txt", "r", encoding="utf-8") as f:
    onboarding_data = f.read()


# ---------------------------
# Utilities (language + http)
# ---------------------------

def detect_language(text: str) -> str:
    """Simple, forgiving detector tuned for this project. Defaults to Italian."""
    t = (text or "").strip().lower()
    if not t:
        return "it"
    italian_hits = [
        "ciao", "grazie", "buongiorno", "buonasera", "salve",
        "nome", "cognome", "documento", "firma", "codice", "residenza", "comune"
    ]
    english_hits = ["hello", "hi", "hey", "thanks", "good morning", "good evening"]
    if any(kw in t for kw in italian_hits):
        return "it"
    if any(kw in t for kw in english_hits):
        return "en"
    return "it"

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
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"📨 Text send -> {phone_number}: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()


# ---------------------------
# Orchestrated GPT Responding
# ---------------------------

def get_state_objects(history):
    """
    Extract any prior 'state' or 'summary' objects from history.
    We store them as messages like: {"from": "state", "text": "{json}"}
    and {"from": "summary", "text": "..."}
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

def build_dialogue_messages(candidate, user_msg: str, lang: str):
    """
    Build the messages list for GPT with:
    - strong System (persona + rules)
    - developer-like Orchestrator instruction to output JSON
    - optional 'summary' context
    - a short recent chat window (to keep tokens lean)
    """
    # Retrieve memory
    history = candidate.history or []
    last_state, last_summary = get_state_objects(history)

    # Short recent window (exclude state/summary to avoid noise)
    recent = [m for m in history if m.get("from") in {"user", "bot", "admin"}][-12:]

    # Persona & style (bilingual)
    base_style_it = """
Sei un assistente per l’onboarding InPlace.it: naturale, empatico, concreto.
Regole:
- Niente frasi robotiche o ripetute; varia sempre le formulazioni.
- Mantieni risposte brevi (1–6 frasi) e specifiche al contesto.
- Ricorda quanto detto prima: se l’utente dice “ok/va bene/grazie”, capisci il contesto e proponi la prossima azione coerente.
- Non chiedere le stesse info due volte se già fornite.
- Offri sempre un prossimo passo chiaro (CTA breve).
- Se l’utente chiede un umano, offri l’escalation.
- Non inventare dati non presenti nel knowledge base.
"""
    base_style_en = """
You are an InPlace.it onboarding assistant: natural, friendly, concrete.
Rules:
- No robotic or repetitive phrasing; always vary wording.
- Keep replies short (1–6 sentences) and context-specific.
- Remember conversation context: if user says “ok/thanks/got it”, move forward coherently.
- Do not ask for info twice if already provided.
- Always end with a clear next step (short CTA).
- If the user asks for a human, offer escalation.
- Do not make up facts not present in the knowledge base.
"""

    system_prompt = f"""
{base_style_it if lang == "it" else base_style_en}

Knowledge base:
{onboarding_data}
"""

    # Orchestrator instruction: force JSON with reply + state
    orchestrator = f"""
You must output ONLY valid JSON with this schema:

{{
  "reply": "string - the user-facing answer, in {'Italian' if lang=='it' else 'English'} only, concise and human-like",
  "intent": "string - inferred user intent (e.g., greeting, proceed_step, document_help, thanks, goodbye, other)",
  "language": "{lang}",
  "next_step": "string - suggested next action (e.g., ask for doc X, confirm step Y)",
  "state_update": {{
      "step": "string or null - current onboarding step if applicable",
      "flags": {{"wants_human": false, "confused": false, "frustrated": false}},
      "notes": "string - brief memory to remember going forward (max 200 chars)"
  }}
}}

Behavioral rules:
- If the message is a greeting or thanks, DO NOT start over; continue from prior context or propose a sensible next step.
- Avoid repeating the same generic greeting or apology across turns.
- Use the prior summary/state to stay consistent.
- Keep tone warm, not formal; no over-apologies.
"""

    messages = [{"role": "system", "content": system_prompt},
                {"role": "system", "content": orchestrator}]

    # Add memory summary if available
    if last_summary:
        messages.append({"role": "system", "content": f"Conversation summary so far:\n{last_summary}"})
    # Add last state if available
    if last_state:
        messages.append({"role": "system", "content": f"State memory:\n{json.dumps(last_state, ensure_ascii=False)}"})

    # Add short recent transcript
    if recent:
        transcript = "\n".join([f"{m['from']}: {m['text']}" for m in recent])
        messages.append({"role": "system", "content": f"Recent transcript:\n{transcript}"})

    # Current user message
    messages.append({"role": "user", "content": user_msg})

    return messages

def summarize_if_needed(candidate):
    """If history is getting long, create/update a rolling summary entry."""
    history = candidate.history or []
    # We’ll summarize when there are > 60 messages and every ~20 new messages
    if len(history) < 60:
        return
    # Extract the last summary index
    last_summary_idx = None
    for i, m in enumerate(history):
        if m.get("from") == "summary":
            last_summary_idx = i
    # Get last 40 relevant messages after the last summary
    start = (last_summary_idx + 1) if last_summary_idx is not None else 0
    window = [m for m in history[start:] if m.get("from") in {"user", "bot", "admin"}][-40:]
    if not window:
        return
    transcript = "\n".join([f"{m['from']}: {m['text']}" for m in window])

    prompt = f"""
Summarize this conversation window into 4–7 bullet points (max 120 words), preserving promises, decisions, user preferences, and current step. Keep {'Italian' if detect_language(transcript)=='it' else 'English'}.

--- WINDOW ---
{transcript}
--- END ---
"""
    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            timeout=12,
            temperature=0.3,
            messages=[{"role": "system", "content": "You produce concise, faithful summaries."},
                      {"role": "user", "content": prompt}]
        )
        summary = res.choices[0].message.content.strip()
        # Upsert the summary (append a new summary entry)
        candidate.history.append({"from": "summary", "text": summary})
        candidate.save()
    except Exception as e:
        print("⚠️ Summary failed:", e)


def orchestrated_reply(candidate, incoming_msg: str, lang: str):
    """
    Single GPT call to produce a JSON with reply + state.
    Stores state memory and returns the reply text.
    """
    messages = build_dialogue_messages(candidate, incoming_msg, lang)
    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            timeout=20,
            temperature=0.7,       # varied, natural
            top_p=1,
            frequency_penalty=0.7, # anti-repetition
            presence_penalty=0.3,
            messages=messages
        )
        raw = res.choices[0].message.content.strip()
        print("🧠 Orchestrator RAW:", raw)

        # Expect JSON only; if it fails, treat as plain reply
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            data = {"reply": raw, "state_update": None, "language": lang, "intent": "other", "next_step": ""}

        reply = (data.get("reply") or "").strip()
        if not reply:
            reply = "Ok." if lang == "en" else "Ok."

        # Persist state update (if any)
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


@csrf_exempt
def meta_webhook(request):
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

        print("Incoming from Meta:", json.dumps(data, indent=2))

        try:
            value = data['entry'][0]['changes'][0]['value']
            if 'messages' in value:
                incoming_msg = value['messages'][0]['text']['body']
                sender_id = value['messages'][0]['from']
                print(f"📨 Message from {sender_id}: {incoming_msg}")

                candidate, _ = Candidate.objects.get_or_create(
                    phone_number=sender_id,
                    defaults={'name': 'Unknown', 'surname': 'Unknown'}
                )

                if candidate.history is None:
                    candidate.history = []
                candidate.history.append({"from": "user", "text": incoming_msg})
                candidate.save()

                # ✅ SMART ESCALATION SYSTEM (Scored) — unchanged
                should_escalate = False
                escalation_reason = ""

                if candidate.status != "escalated":
                    try:
                        chat_history = candidate.history[-5:] if candidate.history else []
                        chat_history_text = "\n".join(
                            [f"{m['from']}: {m['text']}" for m in chat_history] + [f"user: {incoming_msg}"]
                        )

                        classification_prompt = f"""
You are an escalation analyzer for a support chatbot.

You will be given a conversation (last few messages) between a user and a chatbot. Return a JSON response with these four fields:

- frustration_score (0 to 10)
- human_request_score (0 to 10)
- confusion_score (0 to 10)
- repeat_count (0 to 10)

Only escalate if the scores are high.
DO NOT escalate if the user is just asking for help, trying again, or being polite.

Your reply must be only a JSON object like:
{{
  "frustration_score": 7,
  "human_request_score": 2,
  "confusion_score": 8,
  "repeat_count": 3
}}

--- CHAT START ---
{chat_history_text}
--- CHAT END ---
"""

                        result = client.chat.completions.create(
                            model="gpt-4o",
                            timeout=10,
                            messages=[
                                {"role": "system", "content": classification_prompt}
                            ]
                        )

                        response_text = result.choices[0].message.content
                        print("🧠 Raw GPT Escalation Scores:", response_text)

                        scores = json.loads(response_text)
                        f = scores.get("frustration_score", 0)
                        h = scores.get("human_request_score", 0)
                        c = scores.get("confusion_score", 0)
                        r = scores.get("repeat_count", 0)

                        if f >= 7 or h >= 8 or (c >= 8 and r >= 3):
                            should_escalate = True
                            escalation_reason = f"Escalated (F:{f}, H:{h}, C:{c}, R:{r})"

                    except Exception as e:
                        print("⚠️ GPT Escalation Error:", e)

                if should_escalate:
                    candidate.status = "escalated"
                    candidate.escalation_reason = escalation_reason
                    candidate.save()

                    send_escalation_email(candidate)

                    print(f"⛔ Escalated: {escalation_reason}")
                    return JsonResponse({"status": "paused"})

                if candidate.status == 'escalated':
                    print("⛔ Bot paused for this user (already escalated).")
                    return JsonResponse({"status": "paused"})

                # ✅ Orchestrated normal reply (no hard-coded small-talk)
                lang = detect_language(incoming_msg)
                reply = orchestrated_reply(candidate, incoming_msg, lang)

                # Save reply
                candidate.history.append({"from": "bot", "text": reply})
                candidate.status = "replied"
                candidate.save()

                # Send reply
                headers = {
                    "Authorization": f"Bearer {ACCESS_TOKEN}",
                    "Content-Type": "application/json"
                }
                url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
                payload = {
                    "messaging_product": "whatsapp",
                    "to": sender_id,
                    "type": "text",
                    "text": {"body": reply}
                }
                r = requests.post(url, json=payload, headers=headers)
                print("✅ Replied:", r.status_code, r.text)

                # Opportunistic memory compression
                summarize_if_needed(candidate)

        except Exception as e:
            print("❌ Error in meta_webhook main handler:", e)

        return JsonResponse({"status": "received"})


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

        # ✅ Trim chat history to remove old frustration context
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

    # ✅ Define "Completed Onboarding" as having at least 6 bot replies
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
