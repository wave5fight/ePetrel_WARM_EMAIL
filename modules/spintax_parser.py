import re
import random


def parse_spintax(text, seed=None):
    """递归解析 {A|B|C} 格式的 Spintax 文本"""
    rng = random.Random(seed) if seed is not None else random
    pattern = re.compile(r'\{([^{}]+)\}')
    while True:
        match = next((item for item in pattern.finditer(text) if '|' in item.group(1)), None)
        if match is None:
            break
        options = match.group(1).split('|')
        text = text.replace(match.group(0), rng.choice(options), 1)
    return text
