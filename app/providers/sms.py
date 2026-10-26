import logging

from app.config import get_settings
from app.providers.base import SendResult
from app.util import mask_phone

log = logging.getLogger("dh.sms")


class ConsoleSms:
    name = "console"

    def send(self, to, body, dedupe_key):
        log.info("SIMULATED SMS to %s (%d chars)", mask_phone(to), len(body))
        return SendResult(status="simulated", provider="console", sid=None)


class TwilioSms:
    name = "twilio"

    def __init__(self, s):
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client
        self.from_ = s.twilio_from_number
        self.client = Client(s.twilio_account_sid, s.twilio_auth_token, http_client=TwilioHttpClient(timeout=10))

    def send(self, to, body, dedupe_key):
        m = self.client.messages.create(to=to, from_=self.from_, body=body)
        return SendResult(status="accepted", provider="twilio", sid=m.sid)


def get_sms_provider():
    s = get_settings()
    return TwilioSms(s) if s.effective_sms_provider == "twilio" else ConsoleSms()
