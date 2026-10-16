{%- for section, _ in sections.items() %}
{%- for category, val in definitions.items() if category in sections[section] %}

### {{ definitions[category]['name'] }}

{% for text, values in sections[section][category].items() %}
{%- set issues = values | join(", ") %}
{%- for line in text.split("\n") if line.strip() %}
- {{ line.strip() }} ({{ issues }})
{% endfor %}
{%- endfor %}
{%- endfor %}
{%- endfor %}



