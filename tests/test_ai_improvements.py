"""Regression tests for vision decisions and the existing integration contracts.
Model accuracy on real faces is deliberately not claimed by these tests.
"""
import io
import threading
from unittest.mock import Mock, MagicMock

import cv2
import numpy as np
import pytest

from ai.output import concise_answer, spoken_text
from ai.llm import GroqAI
from services.sessions import SessionManager


def test_spoken_output_strips_scores_and_limits_words():
    assert spoken_text('Rambu Kecepatan Maks. 40 km') == 'Rambu Kecepatan Maks. 40 km'
    assert concise_answer('Di depan ada kursi dengan confidence 94.2%.') == 'Di depan ada kursi.'
    assert concise_answer('Ada kursi. Ada kursi.') == 'Ada kursi.'
    text = ' '.join(f'kata{i}' for i in range(200))
    out = spoken_text(text)
    assert 'kata99' in out and 'kata100' not in out and 'lanjutan' in out


def test_groq_context_keeps_sign_without_model_scores(monkeypatch):
    monkeypatch.setattr('ai.llm.settings', Mock(groq_api_key='', groq_model='test'))
    g = GroqAI()
    g._generate = Mock(return_value='Ada rambu Dilarang Parkir.')
    assert g.ask_with_context('Rambu apa di depan?', sign_text='Terdeteksi: Dilarang Parkir.',
                              sign_confidence=.9, sign_reliable=True) == 'Ada rambu Dilarang Parkir.'
    prompt = g._generate.call_args.args[0]
    assert 'Dilarang Parkir' in prompt and 'confidence' not in prompt.lower().replace('deteksi rambu reliable', '')


def test_uncertain_sign_never_sent_to_groq(monkeypatch):
    monkeypatch.setattr('ai.llm.settings', Mock(groq_api_key='', groq_model='test'))
    g = GroqAI()
    g._generate = Mock()
    assert 'belum' in g.ask_with_context('Rambu apa di depan?', sign_text='ragu', sign_reliable=False)
    g._generate.assert_not_called()


def test_reading_text_questions_are_not_sign_questions():
    assert GroqAI.is_sign_question('Rambu apa di depan?')
    assert GroqAI.is_sign_question('lampu merah atau hijau?')
    assert not GroqAI.is_sign_question('Bacakan tulisan di papan itu')


def test_mongodb_ubidots_contracts_without_network(monkeypatch):
    from services.database import Database
    d = Database.__new__(Database)
    client = MagicMock()
    d._ensure_client = Mock(return_value=client)
    assert d.save_interaction({'response': 'Wajah terlihat netral.'})
    collection = client['SPIDER-SENSE']['Interaksi']
    payload = collection.insert_one.call_args.args[0]
    assert payload['response'] == 'Wajah terlihat netral.' and payload['timestamp']
    d.UBIDOTS_TOKEN, d.UBIDOTS_DEVICE_LABEL = 'unit-test-token', 'unit-test-device'
    post = Mock(return_value=Mock())
    monkeypatch.setattr('services.database.requests.post', post)
    assert d._send_ubidots('rabu')
    assert post.call_args.args[0].endswith('/unit-test-device/')
    assert post.call_args.kwargs['json']['rabu']['value'] == 1
    post.return_value.raise_for_status.assert_called_once()


def test_pipeline_sign_mode_returns_sign_fields():
    from ai.pipeline import VisionPipeline
    boxes = [{'box': [1, 2, 3, 4], 'label': 'Polisi Tidur', 'confidence': .8}]
    sign = Mock(analyze=Mock(return_value={'text': 'Terdeteksi: Polisi Tidur.', 'confidence': .8,
                                           'reliable': True, 'boxes': boxes, 'labels': ['Polisi Tidur']}))
    result = VisionPipeline({'sign': sign}).run('rambu', [np.full((480, 640, 3), 128, np.uint8)])
    assert result['sign_text'] == 'Terdeteksi: Polisi Tidur.' and result['sign_boxes'] == boxes
    assert result['result'] == 'Terdeteksi: Polisi Tidur.'


def test_groq_completion_is_cleaned_before_tts(monkeypatch):
    monkeypatch.setattr('ai.llm.settings', Mock(groq_api_key='', groq_model='test', groq_max_output_tokens=220))
    g = GroqAI()
    g.available = True
    g.client = Mock()
    g.client.chat.completions.create.return_value.choices = [Mock(message=Mock(content='Ada kursi dengan confidence 94.2%. Ada kursi dengan confidence 94.2%.'))]
    assert g.ask('Apa di depan?') == 'Ada kursi.'
