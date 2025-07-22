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

                if incoming_msg.lower().strip() in ["help", "support", "talk to human", "human", "operator"]:
                    candidate.status = "escalated"
                    candidate.save()

                if candidate.history is None:
                    candidate.history = []
                candidate.history.append({"from": "user", "text": incoming_msg})
                candidate.save()

                if candidate.status == 'escalated':
                    print("⛔ Bot paused for this user (escalated).")
                    return JsonResponse({"status": "paused"})

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
                    reply = "Si è verificato un errore. Riprova più tardi."
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
        candidate.save()
        return JsonResponse({"resumed": True})
    except Candidate.DoesNotExist:
        return JsonResponse({"resumed": False})
