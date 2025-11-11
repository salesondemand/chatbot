from django.test import TestCase, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch
import pandas as pd

from onboarding import views
from onboarding.models import Candidate


class SendOnboardingTemplateTests(TestCase):
    @patch("onboarding.views.requests.post")
    def test_send_onboarding_template_payload(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"
        mock_post.return_value.json.return_value = {"success": True}

        phone = "393331234567"
        first_name = "Mario"
        company = "ACME S.p.A."
        position = "Operaio Specializzato"

        views.send_onboarding_template(phone, first_name, company, position)

        self.assertTrue(mock_post.called, "requests.post should be called")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertEqual(payload["template"]["name"], "inplace_onboarding_v3")

        body_params = payload["template"]["components"][1]["parameters"]
        expected_values = [first_name, company, position]
        actual_values = [param["text"] for param in body_params]
        self.assertEqual(actual_values, expected_values)

        header = payload["template"]["components"][0]["parameters"][0]
        self.assertEqual(header["document"]["filename"], "Informativa_InPlace.pdf")


class UploadExcelTemplateTests(TestCase):
    def setUp(self):
        self.client = Client()

    @patch("onboarding.views.requests.post")
    @patch("onboarding.views.pd.read_excel")
    def test_upload_excel_uses_three_variables(self, mock_read_excel, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"
        mock_post.return_value.json.return_value = {"success": True}

        mock_read_excel.return_value = pd.DataFrame([
            {
                "name": "Luca",
                "surname": "Bianchi",
                "phone_number": "+39 333 9876543",
                "company_name": "Beta SRL",
                "job_position": "Addetto Magazzino",
            }
        ])

        fake_file = SimpleUploadedFile(
            "candidates.xlsx",
            b"dummy",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post("/upload_excel/", {"file": fake_file})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("added"), 1)

        candidate = Candidate.objects.get(phone_number="393339876543")
        self.assertEqual(candidate.name, "Luca")

        self.assertTrue(mock_post.called, "requests.post should be called during upload")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        body_params = payload["template"]["components"][1]["parameters"]
        values = [param["text"] for param in body_params]
        self.assertEqual(values, ["Luca", "Beta SRL", "Addetto Magazzino"])
