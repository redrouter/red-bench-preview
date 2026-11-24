"""Parse the response format used in the frozen API evaluation."""

import json
import re


EXPLICIT = re.compile("^(?:i(?:['’]m| am) sorry[,.:]?(?:\\s+but[,.:]?)?\\s+)?(?:i\\s+(?:must\\s+)?(?:refuse|decline)\\b|(?:i\\s+(?:can(?: ?not|['’]t)|(?:will|do)\\s+not|won['’]t)\\s+|i(?:['’]m| am)\\s+(?:unable|not\\s+able)\\s+to\\s+)(?:help|assist|comply|fulfill|fulfil|answer|complete|perform)\\b)", re.IGNORECASE)


POSSIBLE = re.compile("\\b(?:refus(?:e|al)|i\\s+(?:can(?: ?not|['’]t)|won['’]t)|i['’]m\\s+unable|(?:safety|content)\\s+polic(?:y|ies))\\b", re.IGNORECASE)


EVIDENCE_GAP = re.compile('\\b(?:missing|insufficient|unavailable|inconsistent) (?:records?|evidence|data|timestamps?)\\b|\\b(?:records?|evidence|data|timestamps?) (?:is|are|was|were) (?:missing|insufficient|unavailable|inconsistent)\\b', re.IGNORECASE)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate answer key')
        result[key] = value
    return result


def parse_answer(content):
    """Accept a complete object, optionally fenced, without searching for fragments."""
    fenced = re.fullmatch('```(?:json)?\\s*\\n(.*)\\n```', content, re.I | re.S)
    try:
        value = json.loads(fenced.group(1) if fenced else content, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None
