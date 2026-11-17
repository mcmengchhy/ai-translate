"""Exact, standalone conversational phrases; never rewrite sentence fragments."""

# Each pair is (neutral modern Khmer, spoken Khmer). Questions retain their
# question mark in the lookup, so a questioning statement is never flattened.
_PHRASES = (
    (("别担心", "不要担心", "don't worry"), ("កុំបារម្ភ។", "កុំបារម្ភអី។")),
    (("我不明白", "i don't understand"), ("ខ្ញុំមិនយល់ទេ។", "ខ្ញុំមិនយល់ទេ។")),
    (("我明白了", "i understand now"), ("ឥឡូវខ្ញុំយល់ហើយ។", "ឥឡូវខ្ញុំយល់ហើយ។")),
    (("我不是这个意思", "i didn't mean that"),
     ("ខ្ញុំមិនមែនចង់និយាយបែបនោះទេ។", "ខ្ញុំមិនមែនចង់និយាយអ៊ីចឹងទេ។")),
    (("我只是开玩笑", "i was just kidding", "i'm just kidding"),
     ("ខ្ញុំគ្រាន់តែនិយាយលេងទេ។", "ខ្ញុំគ្រាន់តែនិយាយលេងទេ។")),
    (("已经太晚了", "it's too late", "it is too late"), ("ហួសពេលហើយ។", "ហួសពេលហើយ។")),
    (("怎么可能?", "how is that possible?"), ("តើវាអាចទៅរួចយ៉ាងដូចម្តេច?", "ម៉េចអាចទៅរួច?")),
    (("你没事吧?", "are you okay?"), ("តើអ្នកមិនអីទេឬ?", "អ្នកមិនអីទេឬ?")),
)
_LOOKUP = {source: translations for sources, translations in _PHRASES for source in sources}
# Chinese question particles/phrases also form questions without punctuation.
for _source in ("怎么可能", "你没事吧"):
    _LOOKUP[_source] = _LOOKUP[_source + "?"]


def native_phrase_translation(source: str, tone: str = "casual") -> str:
    """Use reviewed wording only when the complete source and register match."""
    if tone not in {"standard", "casual"}:
        return ""
    key = source.strip().casefold().replace("’", "'").replace("？", "?")
    # Ellipses and exclamations carry emotion: leave them to the provider.
    if key.endswith(("...", "…", "!", "！")) or "\n" in key or "\r" in key:
        return ""
    key = key.rstrip("。.").strip()
    pair = _LOOKUP.get(key)
    return pair[1 if tone == "casual" else 0] if pair else ""
