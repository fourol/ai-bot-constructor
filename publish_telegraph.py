import urllib.request
import json
import re
import sys

ACCESS_TOKEN = "40481d9817b8fe267c751025f15842aed27c8b5818e2728d64b075510504"
AUTHOR_NAME = "Колледж связи №54 • AI-Интегратор"
AUTHOR_URL = "https://github.com/fourol/ai-bot-constructor"


def parse_inline(text):
    nodes = []
    pattern = re.compile(r'(\*\*.*?\*\*|\`.*?\`|\[.*?\]\(.*?\))')
    parts = pattern.split(text)
    for p in parts:
        if not p:
            continue
        if p.startswith('**') and p.endswith('**') and len(p) >= 4:
            nodes.append({'tag': 'b', 'children': [p[2:-2]]})
        elif p.startswith('`') and p.endswith('`') and len(p) >= 2:
            nodes.append({'tag': 'code', 'children': [p[1:-1]]})
        elif p.startswith('[') and '](' in p and p.endswith(')'):
            m = re.match(r'\[(.*?)\]\((.*?)\)', p)
            if m:
                nodes.append({
                    'tag': 'a',
                    'attrs': {'href': m.group(2)},
                    'children': [m.group(1)],
                })
            else:
                nodes.append(p)
        else:
            nodes.append(p)
    return nodes if nodes else [text]


def format_table(table_lines):
    rows = []
    for line in table_lines:
        line = line.strip()
        if not line or not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if all(re.match(r'^-+$', c) for c in cells):
            continue
        rows.append(cells)

    if not rows:
        return '\n'.join(table_lines)

    num_cols = max(len(r) for r in rows)
    col_widths = [0] * num_cols
    for r in rows:
        for idx, c in enumerate(r):
            col_widths[idx] = max(col_widths[idx], len(c))

    res_lines = []
    header = rows[0]
    sep = (
        '+-'
        + '-+-'.join('-' * col_widths[idx] for idx in range(num_cols))
        + '-+'
    )
    res_lines.append(sep)
    h_str = (
        '| '
        + ' | '.join(
            header[idx].ljust(col_widths[idx])
            if idx < len(header)
            else ' ' * col_widths[idx]
            for idx in range(num_cols)
        )
        + ' |'
    )
    res_lines.append(h_str)
    res_lines.append(sep)
    for r in rows[1:]:
        row_str = (
            '| '
            + ' | '.join(
                r[idx].ljust(col_widths[idx])
                if idx < len(r)
                else ' ' * col_widths[idx]
                for idx in range(num_cols)
            )
            + ' |'
        )
        res_lines.append(row_str)
    res_lines.append(sep)
    return '\n'.join(res_lines)


def parse_markdown_to_nodes(lines):
    nodes = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].rstrip('\r\n')

        if not line.strip():
            i += 1
            continue

        if line.startswith('```'):
            code_lines = []
            i += 1
            while i < n and not lines[i].startswith('```'):
                code_lines.append(lines[i].rstrip('\r\n'))
                i += 1
            i += 1
            nodes.append({'tag': 'pre', 'children': ['\n'.join(code_lines)]})
            continue

        if line.startswith('# '):
            nodes.append({'tag': 'h3', 'children': parse_inline(line[2:].strip())})
            i += 1
            continue
        if line.startswith('## '):
            nodes.append({'tag': 'h3', 'children': parse_inline(line[3:].strip())})
            i += 1
            continue
        if line.startswith('### '):
            nodes.append({'tag': 'h4', 'children': parse_inline(line[4:].strip())})
            i += 1
            continue
        if line.startswith('#### '):
            nodes.append({
                'tag': 'p',
                'children': [
                    {'tag': 'b', 'children': parse_inline(line[5:].strip())}
                ],
            })
            i += 1
            continue

        if line.strip() == '---':
            nodes.append({'tag': 'hr'})
            i += 1
            continue

        if line.startswith('> '):
            nodes.append({
                'tag': 'blockquote',
                'children': parse_inline(line[2:].strip()),
            })
            i += 1
            continue

        if line.startswith('|'):
            table_lines = []
            while i < n and lines[i].strip().startswith('|'):
                table_lines.append(lines[i].rstrip('\r\n'))
                i += 1
            formatted = format_table(table_lines)
            nodes.append({'tag': 'pre', 'children': [formatted]})
            continue

        if line.startswith('- ') or line.startswith('* '):
            ul_items = []
            while i < n and (
                lines[i].startswith('- ') or lines[i].startswith('* ')
            ):
                item_text = lines[i].strip()[2:].strip()
                ul_items.append(
                    {'tag': 'li', 'children': parse_inline(item_text)}
                )
                i += 1
            nodes.append({'tag': 'ul', 'children': ul_items})
            continue

        m_ol = re.match(r'^\d+\.\s+(.*)', line)
        if m_ol:
            ol_items = []
            while i < n:
                m = re.match(r'^\d+\.\s+(.*)', lines[i].strip())
                if m:
                    ol_items.append(
                        {'tag': 'li', 'children': parse_inline(m.group(1))}
                    )
                    i += 1
                else:
                    break
            nodes.append({'tag': 'ol', 'children': ol_items})
            continue

        nodes.append({'tag': 'p', 'children': parse_inline(line.strip())})
        i += 1

    return nodes


def telegra_create_page(title, content_nodes, max_retries=5):
    payload = {
        'access_token': ACCESS_TOKEN,
        'title': title,
        'author_name': AUTHOR_NAME,
        'author_url': AUTHOR_URL,
        'content': json.dumps(content_nodes, ensure_ascii=False),
        'return_content': False,
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.telegra.ph/createPage',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                if not res.get('ok'):
                    raise RuntimeError(f"Telegraph API error: {res}")
                return res['result']
        except Exception as e:
            print(f"  [Attempt {attempt}/{max_retries}] error: {e}")
            if attempt == max_retries:
                raise
            import time
            time.sleep(2 * attempt)


def telegra_edit_page(path, title, content_nodes, max_retries=5):
    payload = {
        'access_token': ACCESS_TOKEN,
        'path': path,
        'title': title,
        'author_name': AUTHOR_NAME,
        'author_url': AUTHOR_URL,
        'content': json.dumps(content_nodes, ensure_ascii=False),
        'return_content': False,
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.telegra.ph/editPage',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                if not res.get('ok'):
                    raise RuntimeError(f"Telegraph API error: {res}")
                return res['result']
        except Exception as e:
            print(f"  [Attempt {attempt}/{max_retries}] error: {e}")
            if attempt == max_retries:
                raise
            import time
            time.sleep(2 * attempt)


def main():
    with open('/home/deck/prod/README.md', 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()

    # Part 1: Sections 1 to 8 (lines 24 to 410)
    p1_lines = lines[24:410]
    # Part 2: Section 9 (lines 410 to 669)
    p2_lines = lines[410:669]
    # Part 3: Sections 10 to 14 (lines 669 to end)
    p3_lines = lines[669:]

    nodes_p1 = parse_markdown_to_nodes(p1_lines)
    nodes_p2 = parse_markdown_to_nodes(p2_lines)
    nodes_p3 = parse_markdown_to_nodes(p3_lines)

    print(f"Creating Part 1 ({len(nodes_p1)} nodes)...")
    res1 = telegra_create_page(
        "КС54 AI-Интегратор: Архитектура, развертывание и API (Часть 1)",
        nodes_p1,
    )
    print("Part 1 URL:", res1['url'])

    print(f"Creating Part 2 ({len(nodes_p2)} nodes)...")
    res2 = telegra_create_page(
        "КС54 AI-Интегратор: Конструктор схем и 11 модулей (Часть 2)", nodes_p2
    )
    print("Part 2 URL:", res2['url'])

    print(f"Creating Part 3 ({len(nodes_p3)} nodes)...")
    res3 = telegra_create_page(
        "КС54 AI-Интегратор: Практикум, экзамен и регламент (Часть 3)", nodes_p3
    )
    print("Part 3 URL:", res3['url'])

    # Master Index Page
    index_nodes = [
        {
            "tag": "p",
            "children": [
                "Официальное техническое руководство и методический справочник для преподавателей и системных администраторов платформы «КС54 AI-Интегратор» (Колледж связи №54)."
            ],
        },
        {"tag": "hr"},
        {"tag": "h3", "children": ["Навигация по разделам руководства"]},
        {
            "tag": "h4",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": res1["url"]},
                    "children": [
                        "Часть 1: Архитектура, развертывание, база данных и API"
                    ],
                }
            ],
        },
        {
            "tag": "ul",
            "children": [
                {"tag": "li", "children": ["1. Общее описание и назначение"]},
                {
                    "tag": "li",
                    "children": ["2. Трехзвенная архитектура комплекса"],
                },
                {
                    "tag": "li",
                    "children": [
                        "3. Системные требования и расчет VRAM (T1000 8GB на 24"
                        " пользователя)"
                    ],
                },
                {
                    "tag": "li",
                    "children": [
                        "4. Порядок развертывания и запуска (Windows 10 /"
                        " Linux)"
                    ],
                },
                {"tag": "li", "children": ["5. Структура проекта"]},
                {
                    "tag": "li",
                    "children": [
                        "6. База данных SQLite (WAL) и схема таблиц"
                    ],
                },
                {
                    "tag": "li",
                    "children": [
                        "7. Модуль RAG (чанкинг 400/50 и ранжирование BM25Okapi)"
                    ],
                },
                {
                    "tag": "li",
                    "children": ["8. Полная спецификация HTTP API"],
                },
            ],
        },
        {
            "tag": "h4",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": res2["url"]},
                    "children": [
                        "Часть 2: Визуальный конструктор и спецификация 11"
                        " модулей"
                    ],
                }
            ],
        },
        {
            "tag": "ul",
            "children": [
                {
                    "tag": "li",
                    "children": [
                        "9.1–9.5 Базовые модули и ядро: Персонаж, Инструкция,"
                        " Мозг Бота (Хаб), Терминал Бота"
                    ],
                },
                {
                    "tag": "li",
                    "children": [
                        "9.6–9.10 Параметры и фильтры: Креативность (Temp),"
                        " Формат данных, Few-Shot, Guardrails, Стиль и правила"
                    ],
                },
                {
                    "tag": "li",
                    "children": [
                        "9.11–9.12 Контекст и интерфейс: Приветствие и кнопки,"
                        " База знаний RAG (прямое подключение к Терминалу)"
                    ],
                },
                {
                    "tag": "li",
                    "children": ["9.13 Сводная матрица коммутации портов"],
                },
            ],
        },
        {
            "tag": "h4",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": res3["url"]},
                    "children": [
                        "Часть 3: Практикум, экзамен, регламент урока и"
                        " диагностика"
                    ],
                }
            ],
        },
        {
            "tag": "ul",
            "children": [
                {
                    "tag": "li",
                    "children": [
                        "10. Профили рабочих мест учащихся (ПК-01..30) и"
                        " изоляция галереи"
                    ],
                },
                {
                    "tag": "li",
                    "children": [
                        "11. Система заданий (Уровни 1-3) и аттестационный"
                        " экзамен по 3 вариантам"
                    ],
                },
                {
                    "tag": "li",
                    "children": [
                        "12. Методический регламент занятия на 90 минут и"
                        " 100-балльный чек-лист"
                    ],
                },
                {
                    "tag": "li",
                    "children": ["13. Типичные ошибки учащихся и их решение"],
                },
                {
                    "tag": "li",
                    "children": [
                        "14. Диагностика неисправностей (сеть, брандмауэр,"
                        " CUDA, порты)"
                    ],
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "p",
            "children": [
                "Репозиторий проекта на GitHub: ",
                {
                    "tag": "a",
                    "attrs": {"href": AUTHOR_URL},
                    "children": ["github.com/fourol/ai-bot-constructor"],
                },
            ],
        },
    ]

    print("Creating Master Index Page...")
    res_index = telegra_create_page(
        "КС54 AI-Интегратор: Техническая документация", index_nodes
    )
    print("Master Index URL:", res_index['url'])

    # Add navigation header & footer to each part and edit
    nav_p1_header = [
        {
            "tag": "p",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": res_index["url"]},
                    "children": ["<- Главное оглавление"],
                },
                " | Часть 1 из 3 | ",
                {
                    "tag": "a",
                    "attrs": {"href": res2["url"]},
                    "children": ["Перейти к Части 2 ->"],
                },
            ],
        },
        {"tag": "hr"},
    ]
    nav_p1_footer = [
        {"tag": "hr"},
        {
            "tag": "p",
            "children": [
                "Следующий раздел: ",
                {
                    "tag": "a",
                    "attrs": {"href": res2["url"]},
                    "children": [
                        "Часть 2: Визуальный конструктор и спецификация 11"
                        " модулей ->"
                    ],
                },
            ],
        },
    ]
    telegra_edit_page(
        res1['path'],
        "КС54 AI-Интегратор: Архитектура, развертывание и API (Часть 1)",
        nav_p1_header + nodes_p1 + nav_p1_footer,
    )

    nav_p2_header = [
        {
            "tag": "p",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": res_index["url"]},
                    "children": ["<- Главное оглавление"],
                },
                " | ",
                {
                    "tag": "a",
                    "attrs": {"href": res1["url"]},
                    "children": ["<- Часть 1"],
                },
                " | Часть 2 из 3 | ",
                {
                    "tag": "a",
                    "attrs": {"href": res3["url"]},
                    "children": ["Перейти к Части 3 ->"],
                },
            ],
        },
        {"tag": "hr"},
    ]
    nav_p2_footer = [
        {"tag": "hr"},
        {
            "tag": "p",
            "children": [
                "Следующий раздел: ",
                {
                    "tag": "a",
                    "attrs": {"href": res3["url"]},
                    "children": [
                        "Часть 3: Практикум, экзамен, регламент урока и"
                        " диагностика ->"
                    ],
                },
            ],
        },
    ]
    telegra_edit_page(
        res2['path'],
        "КС54 AI-Интегратор: Конструктор схем и 11 модулей (Часть 2)",
        nav_p2_header + nodes_p2 + nav_p2_footer,
    )

    nav_p3_header = [
        {
            "tag": "p",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": res_index["url"]},
                    "children": ["<- Главное оглавление"],
                },
                " | ",
                {
                    "tag": "a",
                    "attrs": {"href": res2["url"]},
                    "children": ["<- Часть 2"],
                },
                " | Часть 3 из 3",
            ],
        },
        {"tag": "hr"},
    ]
    nav_p3_footer = [
        {"tag": "hr"},
        {
            "tag": "p",
            "children": [
                "Вернуться к началу: ",
                {
                    "tag": "a",
                    "attrs": {"href": res_index["url"]},
                    "children": ["Главное оглавление руководства"],
                },
            ],
        },
    ]
    telegra_edit_page(
        res3['path'],
        "КС54 AI-Интегратор: Практикум, экзамен и регламент (Часть 3)",
        nav_p3_header + nodes_p3 + nav_p3_footer,
    )

    print("\nSUCCESS! All articles published and cross-linked successfully:")
    print("Master Index:", res_index['url'])
    print("Part 1:      ", res1['url'])
    print("Part 2:      ", res2['url'])
    print("Part 3:      ", res3['url'])


if __name__ == '__main__':
    main()
