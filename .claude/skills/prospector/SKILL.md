---
name: prospector
description: "Scrapes local business leads from Google Maps using Apify and creates an Airtable base with all relevant data and a WhatsApp URL field per lead. Use when the user says /prospector, wants to prospect leads, scrape businesses into Airtable, or build a lead database. Requires: Apify API key, Airtable personal access token, niche and zone."
---

# Skill: Prospector

Scrapes Google Maps leads via Apify → crea una base Airtable `Leads [Nicho] [Zona]` con datos relevantes y un campo WhatsApp con link directo por lead.

## Inputs requeridos

Las credenciales se leen **solo de variables de entorno** (nunca por argumento ni pegadas en el chat):

| Input | Cómo obtenerlo |
|---|---|
| `APIFY_TOKEN` (env) | apify.com → Account → Integrations → API token |
| `AIRTABLE_TOKEN` (env) | airtable.com/create/tokens — scopes: `schema.bases:write`, `data.records:write`, `schema.bases:read` |
| `niche` | Ej: "clínicas dentales", "restaurantes", "inmobiliarias" |
| `zone` | Ej: "Madrid", "Buenos Aires", "Miami" |
| `max_results` | Opcional, default 200 |
| `workspace_id` | Opcional — auto-detectado; si falla, abrí cualquier base en Airtable y mirá la URL |

Si faltan las variables, pedile al usuario que las agregue en la configuración del entorno (no en el chat). Pedí niche/zone si no los dio.

En Claude Code on the web, la política de red del entorno tiene que permitir `api.apify.com`, `api.airtable.com` y `nominatim.openstreetmap.org`.

## Ejecución

1. Instalar dependencias: `pip3 install -q requests phonenumbers`
2. Correr el script incluido en este skill:

```bash
python3 .claude/skills/prospector/prospector.py \
  --niche "NICHE" \
  --zone "ZONE" \
  --max-results 200
```

Opciones: `--max-wait SEGUNDOS` (default 1800; si Apify no termina, el run se aborta para no consumir créditos) y `--workspace-id "wspXXXXXXXX"` si falla la auto-detección.

## Campos que crea en Airtable

| Campo | Tipo |
|---|---|
| Empresa | Texto |
| Teléfono | Teléfono (normalizado a E.164, con código de país) |
| Rating | Número (1 decimal) |
| Reseñas | Número |
| Categoría | Texto |
| Dirección | Texto |
| Ciudad | Texto |
| Web | URL |
| Google Maps | URL |
| WhatsApp | Fórmula URL (clickeable; vacía si no hay teléfono válido) |

### Fórmula del campo WhatsApp

```
IF({Teléfono}, "https://wa.me/" & SUBSTITUTE({Teléfono}, "+", "") & "?text=" & ENCODE_URL_COMPONENT("Hola, ¿hablo con " & {Empresa} & "?"))
```

## Botón WhatsApp verde (paso manual opcional — 30 seg)

La API de Airtable no permite crear campos Button. Para un botón visual:

1. Abrí la base → click en **+** al final de las columnas
2. Tipo: **Button** · Label: `WhatsApp` · Color: verde
3. Action: **Open URL** → pegar la fórmula de arriba
4. Save

## Comportamiento

- Teléfonos en formato local se completan con el código de país del lead (o de la zona). Los que no se pueden validar quedan vacíos y se informa cuántos.
- Se descartan solo los leads cuyo país es explícitamente distinto al de la zona; los que vienen sin país se conservan.
- Limitación conocida: en Argentina, WhatsApp requiere `549` para celulares y Google Maps no distingue celular de fijo, así que esos links pueden fallar.

## Manejo de errores

| Error | Causa | Solución |
|---|---|---|
| Faltan variables de entorno | `APIFY_TOKEN`/`AIRTABLE_TOKEN` no configuradas | Agregarlas en la configuración del entorno |
| Apify 401 | API key inválida | Verificar en apify.com |
| Apify FAILED / 0 resultados | Scraper falló o término sin resultados | Reintentar con nicho más genérico |
| Apify no terminó en N min | Run lento o colgado (ya abortado) | Bajar `--max-results` o subir `--max-wait` |
| Airtable 403 | Token sin permisos | Agregar scopes `schema.bases:write`, `data.records:write`, `schema.bases:read` |
| workspace_id not found | Sin bases existentes para auto-detectar | Crear una base vacía en Airtable primero, o pasar `--workspace-id` |
| CONNECT tunnel failed 403 | Red del entorno bloquea el host | Permitir los dominios en la política de red del entorno |
