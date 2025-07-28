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


def detect_language(text):
    italian_keywords = ["ciao", "nome", "cognome", "documento", "firma", "codice", "residenza", "comune"]
    score = sum(kw in text.lower() for kw in italian_keywords)
    return "it" if score > 1 else "en"


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
                    print(f"⛔ Escalated: {escalation_reason}")
                    return JsonResponse({"status": "paused"})

                if candidate.status == 'escalated':
                    print("⛔ Bot paused for this user (already escalated).")
                    return JsonResponse({"status": "paused"})

                # ✅ CHATBOT REPLY (Normal)
                lang = detect_language(incoming_msg)
                system_prompt = f"""
Sei un assistente virtuale esperto della piattaforma InPlace.it...

Ecco le informazioni da tenere in memoria:
{onboarding_data}
""" if lang == "it" else f"""
You are a virtual assistant for InPlace.it onboarding...

Here is all platform knowledge:
{onboarding_data}
"""

                try:
                    chat_completion = client.chat.completions.create(
                        model="gpt-4o",
                        timeout=15,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": incoming_msg}
                        ]
                    )
                    reply = chat_completion.choices[0].message.content.strip()
                    print("[GPT REPLY]:", reply)
                except Exception as e:
                    reply = "Sorry, something went wrong. Please try again later."
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

