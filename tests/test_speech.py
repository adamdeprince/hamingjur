"""Speech and Voice Lock operations on the Silkie terminal stream."""
from urllib.parse import unquote

import pytest

from hamingjur import silkie
from hamingjur.protocols import FRAME_RE, SpeechAck, SpeechCapabilities, ST, parse_event
from .test_ui import terminal_fixture  # noqa: F401


def test_speech_reports_and_acknowledgements_keep_other_input(term):
    """Correlate host replies with their requests and preserve other frames."""
    term._is_a_tty = True
    term._keyboard_fd = None
    report = silkie.command('speech', 'cap', id=1, available=1, engine='avspeech', pitch_range=0)
    other = silkie.command('speech', 'ack', id=17, status='EINVAL')
    response = silkie.command('speech', 'ack', id=2, status='ENOENT')
    term.ungetch('x' + report + '\x1b[1;1R')
    caps = term.speech.query(timeout=0)
    assert isinstance(caps, SpeechCapabilities)
    assert caps.available and caps.engine == 'avspeech' and not caps.pitch_range
    assert term.speech is term.silkie.speech
    assert term.inkey(timeout=0) == 'x'
    term.ungetch(other + response + '\x1b[1;1R')
    reply = term.speech.clear_style('missing', wait=True, timeout=0)
    assert isinstance(reply, SpeechAck) and reply.status == 'ENOENT'
    assert term.inkey(timeout=0).protocol_event.raw == other


def test_voice_lock_and_all_speech_operations(term):
    """Send styled runs, overrides, audio cues, and queue controls in order."""
    speech = term.speech
    speech.set_style('code.comment', silkie.SpeechStyle(
        voice='Voix française', language='fr-CA', pitch=-40, rate=5, pitch_range=20,
        volume=0, punctuation='all', split_caps=True))
    phrase = 'Café: 50%\n東京' * 10000
    speech.queue(phrase, style='code.comment', options=silkie.SpeechStyle(rate=0, split_caps=False))
    speech.sound('Glass', queued=True)
    speech.silence(250, queued=True)
    speech.dispatch()
    speech.character('😀', interrupt=True)
    speech.speak('Done')
    speech.pause()
    speech.resume()
    speech.stop()
    speech.clear_style('code.comment')
    speech.reset()
    messages = [parse_event(frame) for frame in FRAME_RE.findall(term.stream.getvalue())]
    assert [msg.operation for msg in messages] == [
        'set-style', 'queue', 'sound', 'silence', 'dispatch', 'character', 'speak',
        'pause', 'resume', 'stop', 'clear-style', 'reset',
    ]
    style, queued, sound, silence, _, echo = messages[:6]
    assert unquote(style.fields['voice']) == 'Voix française'
    assert style.fields['language'] == 'fr-CA'
    assert style.fields['volume'] == '0' and style.fields['pitch_range'] == '20'
    assert style.fields['split_caps'] == '1'
    assert queued.fields['rate'] == queued.fields['split_caps'] == '0'
    assert unquote(queued.fields['text']) == phrase
    assert sound.fields['queued'] == silence.fields['queued'] == '1'
    assert silence.fields['duration'] == '250'
    assert echo.fields['interrupt'] == '1'
    assert all('id' not in msg.fields for msg in messages)


@pytest.mark.parametrize('operation,fields', [
    ('character', {'text': 'ab'}),
    ('speak', {'text': ' \n\t '}),
    ('speak', {'text': 'words' + ST}),
    ('queue', {'text': 'word', 'pitch': 101}),
    ('silence', {'duration': -1}),
    ('set-style', {'style': 'two words'}),
    ('set-style', {'style': 'code', 'split_caps': 2}),
])
def test_invalid_speech_commands(operation, fields):
    """Reject malformed speech before writing to the terminal."""
    with pytest.raises(ValueError):
        silkie.speech_command(operation, **fields)
