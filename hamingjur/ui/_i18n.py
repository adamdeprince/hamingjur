"""Per-desktop localization for Hamingjur-owned widget text."""
from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Iterable

DEFAULT_LANGUAGE = 'en-us'


class Message(str):
    """An American English source string with a message ID and deferred formatting."""

    message: str
    values: dict[str, object]

    def __new__(cls, message: str, /, **values: object) -> Message:
        """Keep the English value usable by widgets before they are attached."""
        instance = super().__new__(cls, message.format(**values) if values else message)
        instance.message = message
        instance.values = values
        return instance

    def localize(self, catalog: dict[str, str]) -> str:
        """Render the message template and any nested localized arguments."""
        template = catalog.get(self.message, self.message)
        if not self.values:
            return template
        return template.format(**{
            key: value.localize(catalog) if isinstance(value, Message) else value
            for key, value in self.values.items()
        })


class JoinedMessage(Message):
    """Keep message fragments translatable when assembling a multi-line view."""

    separator: str
    parts: tuple[str, ...]

    def __new__(cls, separator: str, parts: Iterable[str]) -> JoinedMessage:
        """Join English fallback strings while retaining their original fragments."""
        messages = tuple(parts)
        instance = super().__new__(cls, separator.join(messages))
        instance.separator = separator
        instance.parts = messages
        return instance

    def localize(self, catalog: dict[str, str]) -> str:
        """Resolve each fragment before joining it."""
        return self.separator.join(part.localize(catalog) if isinstance(part, Message) else part
                                   for part in self.parts)


@lru_cache(maxsize=None)
def catalogs() -> dict[str, dict[str, str]]:
    """Load the built-in UTF-8 widget translations."""
    with files('hamingjur.ui').joinpath('translations.json').open(encoding='utf-8') as source:
        translations: dict[str, dict[str, str]] = json.load(source)
    return translations


def select_language(preferences: Iterable[str]) -> str:
    """Select the first supported language, accounting for scripts and regions."""
    for language in preferences:
        tag = language.lower()
        primary = tag.split('-', 1)[0]
        if primary == 'en':
            return DEFAULT_LANGUAGE
        if not primary or primary == 'und':
            continue
        variants = tag.split('-')[1:]
        if primary == 'zh':
            traditional = 'hant' in variants or (
                'hans' not in variants and any(region in variants for region in ('tw', 'hk', 'mo')))
            return 'zh-hant' if traditional else 'zh-hans'
        if primary == 'pt' and 'br' in variants:
            return 'pt-br'
        if primary == 'no':
            primary = 'nb'
        while '-' in tag:
            if tag in catalogs():
                return tag
            tag = tag.rsplit('-', 1)[0]
        if primary in catalogs():
            return primary
    return DEFAULT_LANGUAGE


def translate(text: str, language: str) -> str:
    """Translate marked library text and preserve caller-supplied strings."""
    if isinstance(text, Message):
        return str(text) if language == DEFAULT_LANGUAGE else text.localize(catalogs()[language])
    return text
