from .domain import ValidationError


def parse_options(tokens: list[str], allowed: set[str]) -> dict[str, str]:
    options = {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator or key not in allowed or not value or key in options:
            raise ValidationError(f"参数格式错误或重复：{key}。包含空格的参数请用引号包住。")
        options[key] = value
    return options


def required(options: dict, key: str) -> str:
    if key not in options:
        raise ValidationError(f"缺少参数：{key}=...")
    return options[key]
