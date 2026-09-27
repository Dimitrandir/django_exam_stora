"""AI-assisted quantity suggestions for a single supplier/period order (see
STORA.orders.views.order_ai_suggest). Reuses STORA.reports.ai_service's
already-built read-only-SQL/Claude plumbing (same schema description, same
enforced-read-only DB connection, same run_sql tool) rather than
duplicating it -- the only real difference is the task: instead of
answering a free-text question, this asks Claude to recommend a restock
QUANTITY per candidate product and end with a JSON block, which gets
parsed and handed back to fill the compose screen's Requested Qty column.
Nothing here is ever saved on its own -- the caller (order_new's own POST)
is still what actually creates OrderItems, same as a manually-typed
quantity would."""

import json
import re

from django.conf import settings

import anthropic

from STORA.reports.ai_service import AIReportsNotConfigured, _execute_readonly, _format_rows_for_model, describe_schema

MAX_TURNS = 6
MAX_ROWS_RETURNED_TO_MODEL = 200

_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*(\[.*?\])\s*```', re.DOTALL)

TOOLS = [
    {
        'name': 'run_sql',
        'description': (
            'Execute one read-only SQL SELECT statement against the shop database '
            'and get back the matching rows. The connection is enforced read-only '
            'at the database level, so this is safe to use freely to explore data.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'A single SQL SELECT statement.'},
            },
            'required': ['query'],
        },
    },
]


def _system_prompt(supplier_name, positions, start_date, end_date, candidates):
    position_names = {1: 'primary (1st)', 2: 'secondary (2nd)', 3: 'tertiary (3rd)'}
    position_label = ' / '.join(position_names.get(p, str(p)) for p in positions)
    candidate_lines = '\n'.join(f'  - id={p["id"]}: {p["name"]} (code {p["internal_code"]})' for p in candidates)
    return f"""You are the restock-order assistant built into STORA, a small retail
shop's inventory/sales management system. You recommend how many units to
order from one supplier for a "заявка" (restock request) a member of staff
is drafting on screen right now.

You have exactly one tool, `run_sql`. It runs against a PostgreSQL role
that is enforced read-only AT THE DATABASE LEVEL -- every table is
visible, but any write statement is physically rejected before it could
ever do anything.

Database schema (PostgreSQL, real table/column names -- use these
directly in your SQL):

{describe_schema()}

Task: recommend a restock quantity for EVERY product in this candidate
list (products supplied by "{supplier_name}" as their {position_label}
supplier) -- base it on how much of each sold between {start_date} and
{end_date} (products_saleitems joined to sales_saleattributes), and on
recent delivery sizes/frequency from this same supplier
(deliveries_deliveryitems joined to deliveries_deliveryattributes) if
that helps judge a sensible order size (e.g. round to typical case/pack
sizes if the delivery history suggests one). A product with zero sales in
the period should generally get 0 -- don't invent demand that isn't in
the data.

Candidate products (use these exact ids):
{candidate_lines}

Once you've looked at what you need, give a short plain-language summary
of your reasoning (Bulgarian, since that's how this shop is run), then
end your reply with EXACTLY ONE fenced ```json code block containing a
JSON array covering every candidate id above, in this shape:
[{{"product_id": 123, "quantity": 6}}, ...]
quantity is a plain number (whole or fractional, whatever suits the
product) -- not a string, no units, no extra text inside the code block."""


def _extract_quantities(text):
    match = _JSON_BLOCK_RE.search(text or '')
    if not match:
        return []
    try:
        parsed = json.loads(match.group(1))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    result = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            product_id = int(entry.get('product_id'))
            quantity = float(entry.get('quantity'))
        except (TypeError, ValueError):
            continue
        result.append({'product_id': product_id, 'quantity': quantity})
    return result


def suggest_order_quantities(supplier_name, positions, start_date, end_date, candidates):
    """`candidates` is a list of {'id', 'name', 'internal_code'} dicts (the
    products already shown on the compose screen for this supplier/
    position(s)). Returns a list of {'product_id', 'quantity'} for
    whichever of them Claude covered in its final JSON block (callers
    should treat a missing id as "no suggestion", not assume 0). Raises
    AIReportsNotConfigured if no API key is set."""
    api_key = settings.ANTHROPIC_API_KEY
    if not api_key:
        raise AIReportsNotConfigured
    if not candidates:
        return []

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _system_prompt(supplier_name, positions, start_date, end_date, candidates)
    messages = [{'role': 'user', 'content': 'Recommend order quantities for the candidates listed in your instructions.'}]

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=settings.AI_REPORTS_MODEL,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        tool_uses = [block for block in response.content if block.type == 'tool_use']
        if not tool_uses:
            answer = '\n'.join(block.text for block in response.content if block.type == 'text').strip()
            return _extract_quantities(answer)

        messages.append({'role': 'assistant', 'content': response.content})
        tool_results = []
        for tool_use in tool_uses:
            query = (tool_use.input or {}).get('query', '')
            try:
                columns, rows = _execute_readonly(query, MAX_ROWS_RETURNED_TO_MODEL)
                result_text = _format_rows_for_model(columns, rows)
            except Exception as exc:
                result_text = f'Error running that query: {exc}'
            tool_results.append({'type': 'tool_result', 'tool_use_id': tool_use.id, 'content': result_text})
        messages.append({'role': 'user', 'content': tool_results})

    return []
