SUPERSCRIPT_DIGITS = str.maketrans({
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
})


def normalize_grade_code(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    prefix_chars: list[str] = []
    suffix_digits: list[str] = []
    digit_mode = False

    for char in text:
        if char.isdigit():
            digit_mode = True
            suffix_digits.append(char)
            continue

        if digit_mode:
            return text
        prefix_chars.append(char)

    if not suffix_digits:
        return text

    return "".join(prefix_chars) + "".join(suffix_digits).translate(SUPERSCRIPT_DIGITS)


def normalize_grade_fields(instance, *field_names: str) -> None:
    for field_name in field_names:
        if hasattr(instance, field_name):
            setattr(instance, field_name, normalize_grade_code(getattr(instance, field_name, "")))
