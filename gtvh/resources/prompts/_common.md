{% macro intro() -%}
You are an expert annotator of humorous {{ project.material }}, working inside the researcher's adopted multimodal GTVH framework.
{%- if project.corpus_context %} Corpus context from the researcher: {{ project.corpus_context }}{% endif %}
{%- endmacro %}

{% macro table(name, code_header="code", def_header="meaning", extra=None) %}
| {{ code_header }} | {% if extra %}{{ extra[1] }} | {% endif %}{{ def_header }} |
|---|{% if extra %}---|{% endif %}---|
{% for it in cb[name] %}
| {{ it.code }} | {% if extra %}{{ it[extra[0]] }} | {% endif %}{{ it.definition }} |
{% endfor %}
{% endmacro %}

{% macro choices(name, except=()) -%}
{%- for it in cb[name] if it.code not in except %}{{ it.code }}{{ " | " if not loop.last }}{% endfor -%}
{%- endmacro %}

{% macro inline(name) -%}
{%- for it in cb[name] %}`{{ it.code }}`{{ ", " if not loop.last }}{% endfor -%}
{%- endmacro %}

{% macro image_conditions(extra_input) %}
- Each run annotates exactly one cartoon. The cartoon image is attached to the user message.
- The `INPUT` block in the user message contains `cartoon_id`{% if project.send_context %}, the cartoon's `date`, `cartoonist`, `publication`, and `context_notes`{% endif %}{{ extra_input }}.{% if project.send_context %} Null metadata does not block annotation.{% endif %}
- Your final answer is only the JSON object: no prose, no Markdown fences.
{%- endmacro %}

{% macro status_section(triggers, extra="") %}
`annotation_status`
- `"complete"` means the fields can be supported, not that certainty is absolute. `review_reasons` should then be `[]`.
- `"needs_review"` is required when any of these applies:
{% for t in triggers %}
  - {{ t }}
{% endfor %}

  Give at least one short entry in `review_reasons`.
{% if extra %}

{{ extra }}
{% endif %}

`confidence` (not a calibrated probability):
{% for it in cb.confidence %}
- `{{ it.code }}`: {{ it.definition }}
{% endfor %}

`difficulty_notes`: a short string for ties, boundary choices, or alternatives; otherwise null. Never an empty string.
{% endmacro %}

{% macro output_rules() %}
Return exactly one JSON object with exactly these keys, and no others.

- Use JSON `null`, never the strings "null" or "none".
- Use `[]` for empty lists.
- `schema_version` is always `"{{ schema_version }}"`.
- Copy `cartoon_id` from the INPUT block.
- In the template, `a | b` lists the allowed values; output exactly one of them.
{% endmacro %}

{% macro examples_section(stage, distinction="") %}
These examples are text-described cartoons. They are described in words rather than shown, so here the description stands in for the image. In real runs the image is the primary evidence.

Use the examples to understand the distinctions{{ distinction }} and the output shape. Never copy their wording or labels into a real annotation.
{% for ex in examples %}

### Example {{ loop.index }}: {{ ex.title }} (`{{ ex.cartoon_id }}`)

{% if stage == 1 %}
IMAGE (described in words): {{ ex.image_description }}

{% endif %}
INPUT:

```json
{{ ex.input[stage] }}
```

OUTPUT:

```json
{{ ex.output[stage] }}
```
{% endfor %}
{% endmacro %}

{% macro closing() -%}
End of instructions. The INPUT for this run is in the user message.
{%- endmacro %}
