"""Canonical names of the ЖК (residential complexes) and their addresses.

Yandex SpeechKit v3 has no custom-vocabulary API, so it mangles these complex
names and (especially) the Kazakh-language toponyms in the addresses. The LLM
correction pass (:mod:`AtamuraOKK.glossary.llm_correct`) feeds this list to the
model as the authoritative spelling to enforce. This module is pure data with
zero dependencies, so both pipelines can import it freely.

The complex→address pairing comes straight from the operation's target list. Two
deliberate facts to preserve when editing: «Атмосфера 2» shares «Атмосфера»'s
address (same site, second phase), and «Discovery» is the Жана Куат plot.
"""

from __future__ import annotations

from dataclasses import dataclass

_ATMOSFERA_ADDRESS = (
    "г. Алматы, Турксибский р-н, мкр. Нуршашкан, ул. Алатау 36 "
    "(ул. Бухтарминская–Кульджинский тракт), рядом с новым Мол Апортом"
)


@dataclass(frozen=True, slots=True)
class Complex:
    """One residential complex: its canonical name and full canonical address."""

    zhk: str
    address: str


COMPLEXES: tuple[Complex, ...] = (
    Complex("Атмосфера", _ATMOSFERA_ADDRESS),
    Complex(
        "Аура",
        "мкр. Алатау (бывш. ИЯФ), Талгарский р-н, с. Тузусай, "
        "ул. Сырым Датулы, 100а",
    ),
    Complex(
        "Керуен",
        "с. Туздыбастау, ул. Аныракай (Кульджинский тракт, напротив с. Гулдала, "
        "рядом с рынком «Султан»), Талгарский р-н",
    ),
    Complex(
        "Таунхаус «Аксай резорт»",
        "с. Кыргауылды (верхняя Каскеленская трасса, выше рынка Ак-Тилек), "
        "Карасайский р-н, ул. Тамаша, 1В",
    ),
    Complex(
        "Браво",
        "Алматинская обл., Илийский р-н, Энергетический с.о., с. Отеген Батыр, "
        "ул. Ілияс Жансүгіров, уч. 109Д",
    ),
    Complex("Атмосфера 2", _ATMOSFERA_ADDRESS),
    Complex("Discovery", "Алматинская обл., Талгарский р-н, Жана Куат"),
)

# Hard-to-transcribe toponyms (mostly Kazakh) called out explicitly to the model,
# because they are what Yandex most often mangles inside the addresses above.
TOPONYMS: tuple[str, ...] = (
    "Нуршашкан",
    "Тузусай",
    "Сырым Датулы",
    "Аныракай",
    "Туздыбастау",
    "Кыргауылды",
    "Тамаша",
    "Отеген Батыр",
    "Ілияс Жансүгіров",
    "Жана Куат",
    "Кульджинский тракт",
    "Каскеленская трасса",
)


def build_reference_text() -> str:
    """Render the glossary as a compact, deterministic Russian reference block.

    Deterministic (fixed order, no timestamps) so the system prompt is byte-stable
    across calls — a prerequisite for prompt-caching it later.
    """
    names = "; ".join(c.zhk for c in COMPLEXES)
    addresses = "\n".join(f"- {c.zhk} — {c.address}" for c in COMPLEXES)
    toponyms = ", ".join(TOPONYMS)
    return (
        f"КАНОНИЧЕСКИЕ НАЗВАНИЯ ЖК: {names}\n\n"
        f"КАНОНИЧЕСКИЕ АДРЕСА:\n{addresses}\n\n"
        f"ТОПОНИМЫ (писать строго в этом написании): {toponyms}"
    )
