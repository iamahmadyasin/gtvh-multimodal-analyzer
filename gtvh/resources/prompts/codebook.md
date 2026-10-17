# Codebook {{ "drafting" if mode == "draft" else ("development" if mode == "induce" else "consolidation") }}: {{ dimension.title }}

You help a researcher build a coding scheme for a corpus study of humorous {{ project.material }} analysed with the multimodal General Theory of Verbal Humour (GTVH).

{{ dimension.explanation }}

The categories will be used to code every cartoon in the corpus, so that the results can be counted and compared. A good codebook:
- has mutually exclusive categories at one consistent level of generality;
- covers the material, with categories that each hold a meaningful share of it, so that `other` stays small (`other` is added automatically; do not propose it);
- names each category with a short snake_case `code` and gives a one-sentence `definition` that says what belongs in it and, where useful, what does not;
- is relevant to the research questions, without forcing material into categories it does not fit.

{% if mode == "draft" -%}
## Task

Draft an initial set of {{ dimension.plural }} for this corpus before any cartoon has been annotated. Base it on the corpus description and research questions below and on what is typical of such material. Aim for about {{ target_size }} categories. In `includes`, give two to four typical example {{ dimension.examples }} for each category. Use `notes` for assumptions the researcher should check.

The researcher will review and edit the draft and should test it on a pilot sample.

## Corpus

- Name: {{ project.name }}
- Description: {{ project.description or "(none given)" }}
- Material: {{ project.material }}
{% if project.research_questions %}- Research questions:
{% for q in project.research_questions %}  - {{ q }}
{% endfor %}{% endif %}
{%- elif mode == "induce" -%}
## Task

Below are the free-text {{ dimension.examples }} recorded for the cartoons of this corpus, each with the number of cartoons it occurs in{{ dimension.context_note }}. Group them into categories. Aim for about {{ target_size }} categories, fewer if the material is homogeneous.

- In `includes`, list every input label you assign to the category, copied exactly. Assign every input label to exactly one category, except labels too idiosyncratic for any category, which you leave out.
- Base the categories on the labels, not on what you expect such a corpus to contain.
- Use `notes` for boundary decisions the researcher should check.

## Corpus

- Name: {{ project.name }}
- Description: {{ project.description or "(none given)" }}
{% if project.research_questions %}- Research questions:
{% for q in project.research_questions %}  - {{ q }}
{% endfor %}{% endif %}
## Labels

{{ items }}
{%- else -%}
## Task

The labels of this corpus were grouped in separate batches, so the batches below contain overlapping and differently named categories. Consolidate them into one codebook of about {{ target_size }} categories.

- Merge categories that cover the same ground; split or rename where that gives a clearer, more consistent scheme.
- In `includes`, list all the labels of the merged categories, copied exactly. Keep every label that the batches assigned.
- Use `notes` for boundary decisions the researcher should check.

## Batches

{{ items }}
{%- endif %}

## Output

Return only one JSON object, no prose and no Markdown fences:

```
{
  "categories": [
    { "code": "snake_case_code", "definition": "<one sentence>", "includes": ["<label>", "..."] }
  ],
  "notes": "<short> | null"
}
```
