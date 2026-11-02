"""The envelope around remote text the model reads.

Log bytes and source are authored by the change under investigation. The model
is told (prompts.UNTRUSTED_RULE) that text inside the envelope is data. That
only holds if the text cannot close the envelope itself, so any tag-like
sequence naming the envelope is defanged before wrapping.
"""

import re

UNTRUSTED_OPEN, UNTRUSTED_CLOSE = "<untrusted_ci_output>", "</untrusted_ci_output>"

# Open or close tag, any case, with stray whitespace or attributes: `< /Untrusted_CI_Output >`.
_TAG = re.compile(r"<(\s*/?\s*untrusted_ci_output)", re.IGNORECASE)


def envelope(text: str) -> str:
    """Wrap `text` as data; a literal envelope tag inside it is escaped to `&lt;`."""
    return f"{UNTRUSTED_OPEN}\n{_TAG.sub(r'&lt;\1', text)}\n{UNTRUSTED_CLOSE}"
