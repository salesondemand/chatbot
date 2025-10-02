# onboarding/views.py

import os
import json
import requests
import pandas as pd
import random
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


def detect_language(text):
    italian_keywords = ["ciao", "nome", "cognome", "documento", "firma", "codice", "residenza", "comune"]
    score = sum(kw in text.lower() for kw in italian_keywords)
    return "it" if score > 1 else "en"


# -----------------------------
# ✨ Human-like small-talk layer
# -----------------------------
random.seed()

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

def smart_lang(text: str):
    return "it" if detect_language(text) == "it" else "en"

SMALLTALK_PATTERNS = {
    "greeting": ["hi", "hello", "hey", "ciao", "buongiorno", "salve", "hey there"],
    "thanks": ["thanks", "thank you", "grazie", "thx", "thanks a lot", "mille grazie"],
    "bye": ["bye", "goodbye", "arrivederci", "ciao ciao", "see ya", "see you"],
    "ok": ["ok", "okay", "va bene", "perfetto", "done", "got it", "ricevuto", "sure"]
}

SMALLTALK_RESPONSES = {
    "it": {
        "greeting": [
            "Ciao {name}! 👋 Come stai? Vuoi iniziare l’onboarding ora?",
            "Ehi {name}! 😊 Sono qui per aiutarti con InPlace. Da dove partiamo?",
            "Ciao! Se vuoi, posso guidarti passo-passo. Preferisci iniziare o fare domande?"
        ],
        "thanks": [
            "Di nulla! 🙌 Se vuoi, posso procedere con il prossimo passaggio.",
            "Con piacere! Hai bisogno di altro prima di continuare?",
            "Felice di aiutarti. Pronti a proseguire?"
        ],
        "bye": [
            "A presto {name}! 👋 Se ti serve, scrivimi quando vuoi.",
            "Va bene, ci sentiamo! Buona giornata. 🌟",
            "Grazie a te! Quando vuoi riprendiamo da dove eravamo."
        ],
        "ok": [
            "Perfetto! Vuoi che parta con il primo step?",
            "Ricevuto. Procedo col prossimo passaggio?",
            "Ottimo! Dimmi quando sei pronto/a a iniziare."
        ]
    },
    "en": {
        "greeting": [
            "Hey {name}! 👋 How’s it going? Ready to start onboarding?",
            "Hi! 😊 I’m here to help with InPlace. Want me to guide you step-by-step?",
            "Hello {name}! We can begin now or I can answer quick questions first."
        ],
        "thanks": [
            "You’re welcome! 🙌 Shall we continue to the next step?",
            "Anytime! Need anything else before we move on?",
            "Glad to help. Ready to proceed?"
        ],
        "bye": [
            "Talk soon, {name}! 👋 Ping me anytime.",
            "No worries—have a great day! 🌟",
            "Thanks! We’ll pick up right where we left off."
        ],
        "ok": [
            "Great! Want me to start with step one?",
            "Got it. Should I move to the next step?",
            "Awesome—say the word when you’re ready."
        ]
    }
}

FIRST_WELCOME = {
    "it": [
        "Ciao {name}! 👋 Sono il tuo assistente InPlace. Preferisci iniziare subito o hai domande veloci?",
        "Benvenuto/a! Posso guidarti passo-passo con documenti e firme. Da dove partiamo?"
    ],
    "en": [
        "Hey {name}! 👋 I’m your InPlace assistant. Want to start now or ask a quick question first?",
        "Welcome! I can guide you step-by-step through docs and signatures. Where should we begin?"
    ]
}

def match_smalltalk(text: str):
    t = text.strip().lower()
    # short messages are more likely small-talk
    if len(t) <= 60:
        for intent, keywords in SMALLTALK_PATTERNS.items():
            for kw in keywords:
                if kw in t:
                    return intent
    return None


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

                # 🔹 Human-like first inbound welcome (before escalation/GPT)
                lang = smart_lang(incoming_msg)
                display_name = (candidate.name or "").strip() or ("Amico" if lang == "it" else "Friend")

                is_first_inbound = len(candidate.history) == 1  # we just appended the first user msg
                if is_first_inbound:
                    try:
                        text = random.choice(FIRST_WELCOME.get(lang, []))
                        text = text.format(name=display_name)
                        send_text_message(sender_id, text)
                        candidate.history.append({"from": "bot", "text": text})
                        candidate.status = "replied"
                        candidate.save()
                        # Stop here to avoid double reply on the very first "hi"
                        return JsonResponse({"status": "welcomed"})
                    except Exception as e:
                        print(f"⚠️ First-welcome send failed: {e}")

                # 🔹 Human-like small-talk interception (hi/thanks/ok/bye)
                intent = match_smalltalk(incoming_msg)
                if intent:
                    try:
                        choices = SMALLTALK_RESPONSES.get(lang, {}).get(intent, [])
                        if choices:
                            text = random.choice(choices).format(name=display_name)
                            send_text_message(sender_id, text)
                            candidate.history.append({"from": "bot", "text": text})
                            candidate.status = "replied"
                            candidate.save()
                            # Short-circuit: small-talk answered, no need for GPT
                            return JsonResponse({"status": "smalltalk"})
                    except Exception as e:
                        print(f"⚠️ Smalltalk send failed: {e}")

                # ✅ SMART ESCALATION SYSTEM (Scored)
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

- frustration_score (0 to 10): how angry, annoyed, or upset the user seems
- human_request_score (0 to 10): how much the user is asking to talk to a human
- confusion_score (0 to 10): how unclear or lost the user seems
- repeat_count (0 to 10): how many times the user seems to have asked the same thing

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

                # ✅ CHATBOT REPLY (Normal): upgraded style prompt + anti-repetition
                lang = lang  # keep previously detected language
                base_style_it = """
Sei un assistente InPlace.it.
Stile: naturale, empatico, conciso. Evita frasi robotiche o ripetitive.
Varia le formulazioni: non ripetere la stessa frase di saluto o chiusura.
Usa frasi brevi e proponi sempre il prossimo passo.
Non scusarti se non necessario. Non inventare dati.
Se l’utente chiede un umano, offri l’escalation.
"""
                base_style_en = """
You are an InPlace.it assistant.
Style: natural, friendly, concise. Avoid robotic or repetitive phrasing.
Vary wording: never repeat the same greeting or closing.
Use short sentences and always offer the next step.
Don’t over-apologize. Don’t make up facts.
If the user asks for a human, offer escalation.
"""

                system_prompt = f"""
{base_style_it if lang == "it" else base_style_en}

Knowledge base:
{onboarding_data}

When replying:
- Detect the user’s intent (onboarding step, docs, status, support).
- Keep replies to 1–3 short paragraphs max.
- End with a helpful next action (e.g., “Vuoi iniziare dal documento X?” / “Shall we start with document X?”).
"""

                try:
                    chat_completion = client.chat.completions.create(
                        model="gpt-4o",
                        timeout=15,
                        temperature=0.7,
                        top_p=1,
                        frequency_penalty=0.6,
                        presence_penalty=0.2,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": incoming_msg}
                        ]
                    )
                    reply = chat_completion.choices[0].message.content.strip()
                    print("[GPT REPLY]:", reply)
                except Exception as e:
                    reply = "Sorry, something went wrong. Please try again later." if lang == "en" else "Spiacente, si è verificato un errore. Riprova più tardi."
                    print("[GPT ERROR]:", e)

                candidate.history.append({"from": "bot", "text": reply})
                candidate.status = "replied"
                candidate.save()

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
