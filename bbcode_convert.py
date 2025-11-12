import re


# 将单个 BBCode 转换为 HTML，先不添加 CSS 样式
def bbcode_to_html(text: str) -> str:
    # 定义 BBCode 转换规则
    rules = [
        (r"\[b\](.*?)\[/b\]", r"<strong>\1</strong>"),
        (r"\[i\](.*?)\[/i\]", r"<em>\1</em>"),
        (r"\[u\](.*?)\[/u\]", r"<u>\1</u>"),
        (r"\[s\](.*?)\[/s\]", r"<del>\1</del>"),
        (r"\[url=(.*?)\](.*?)\[/url\]", r'<a href="\1">\2</a>'),
        (r"\[img\](.*?)\[/img\]", r'<img src="\1" alt="" />'),
        (r"\[quote\](.*?)\[/quote\]", r"<blockquote>\1</blockquote>"),
        (r"\[code\](.*?)\[/code\]", r"<pre><code>\1</code></pre>"),
        (r"\[color=(.*?)\](.*?)\[/color\]", r'<span style="color:\1">\2</span>'),
        (r"\[size=(.*?)\](.*?)\[/size\]", r'<span style="font-size:\1">\2</span>'),
    ]

    # 逐个应用规则，为应对标签嵌套，多次应用规则
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text, flags=re.DOTALL | re.IGNORECASE)
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text, flags=re.DOTALL | re.IGNORECASE)
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text, flags=re.DOTALL | re.IGNORECASE)

    return text
