"""Validate the optional joint audio/video playout lead."""


def target_latency_ms(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 4:
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2000:
        raise ValueError('invalid_target_latency_ms')
    return value
