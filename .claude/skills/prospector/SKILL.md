---
name: prospector
description: "Scrapes local business leads from Google Maps using Apify and creates a qualified Airtable base (priority score, digital-presence diagnosis, sales status) with a WhatsApp URL field per lead. Use when the user says /prospector, wants to prospect leads, scrape businesses into Airtable, or build a lead database. Has presets for Alcance Isleño (Tenerife, hostelería, cita previa). Requires APIFY_TOKEN and AIRTABLE_TOKEN env vars, plus sector/niche and zone."
---

# Skill: Prospector

Scrapes Google Maps leads via Apify → crea una base Airtable `Leads [Sector] [Zona]` con cada lead **calificado** según lo que le falta de presencia digital (web, ficha de Google, reseñas), un diagnóstico de una línea, estado comercial y link de WhatsApp.

Pensado para Alcance Isleño (alcanceisleno.com): presencia digital para hostelería y negocios con cita previa en Tenerife. El diagnóstico de cada lead es el "diagnóstico gratuito" que ofrece la web, listo para llevar a la visita.

## Inputs requeridos

Las credenciales se leen **solo de variables de entorno** (nunca por argumento ni pegadas en el chat):

| Input | Cómo obtenerlo |
|---|---|
| `APIFY_TOKEN` (env) | apify.com → Account → Integrations → API token |
| `AIRTABLE_TOKEN` (env) | airtable.com/create/tokens — scopes: `schema.bases:write`, `schema.bases:read`, `data.records:write`, `data.records:read` |
| Qué buscar | `--sector hosteleria` (bares, restaurantes, cafeterías), `--sector cita-previa` (barberías, peluquerías, centros de estética, salones de uñas) y/o `--niche "texto libre"`. Repetibles. |
| Dónde | `--zone-preset tenerife` (los 31 municipios, filtrando códigos postales 380xx–386xx) y/o `--zone "Adeje, Tenerife"`. Repetibles. |

Si faltan las variables, pedile al usuario que las agregue en la configuración del entorno (no en el chat).

En Claude Code on the web, la política de red del entorno tiene que permitir `api.apify.com` y `api.airtable.com` (y `nominatim.openstreetmap.org` si se usa una zona sin preset ni `--country`).

## Ejecución

1. Instalar dependencias: `pip3 install -q requests phonenumbers`
2. **Siempre primero `--dry-run`** para ver cuántas búsquedas y cuántos lugares como máximo se van a cobrar en Apify, y confirmarlo con el usuario.
3. Correr:

```bash
# Prueba chica recomendada para empezar
python3 .claude/skills/prospector/prospector.py \
  --sector hosteleria --zone "Puerto de la Cruz, Tenerife" --country ES --max-per-search 30

# Toda la isla (217 búsquedas con los dos sectores: requiere --yes)
python3 .claude/skills/prospector/prospector.py \
  --sector hosteleria --sector cita-previa --zone-preset tenerife --max-per-search 20 --dry-run
```

| Opción | Uso |
|---|---|
| `--max-per-search N` | Lugares por búsqueda (nicho × zona). Default 50. |
| `--base-id appXXXX` | Agrega a una base existente sin duplicar (por Place ID). Sirve para ir municipio por municipio. |
| `--dry-run` | Lista las búsquedas y el máximo de lugares sin ejecutar nada. |
| `--yes` | Necesario si el máximo supera 1000 lugares. |
| `--country ES` / `--postal-regex` | País y filtro de código postal para zonas sin preset. |
| `--max-wait S` | Default 3600; si Apify no termina, el run se aborta. |
| `--workspace-id wspXXXX` | Si falla la auto-detección. |

## Calificación

Cada lead suma puntos por lo que le falta (lo que vende Alcance Isleño):

| Señal | Puntos |
|---|---|
| Sin web | +3 |
| Solo redes o plataforma (Instagram, Facebook, Booksy, Fresha, TheFork…) | +2 |
| Ficha de Google sin reclamar | +2 |
| Menos de 30 reseñas | +2 |
| 30–99 reseñas | +1 |
| Rating menor a 4,2 | +1 |

**Prioridad:** Alta ≥ 5 · Media 3–4 · Baja < 3. Los registros se suben ordenados de mayor a menor puntuación.

**Diagnóstico:** frase de una línea con las señales, ej. `Sin web · ficha sin reclamar · 8 reseñas · rating 3,9`.

Se descartan duplicados (mismo lugar en varias búsquedas), negocios cerrados definitivamente, otros países y códigos postales fuera de zona. Los cerrados temporalmente se conservan con la nota en el diagnóstico.

## Campos que crea en Airtable

| Campo | Tipo |
|---|---|
| Empresa | Texto |
| Prioridad | Selección (Alta / Media / Baja) |
| Puntuación | Número |
| Diagnóstico | Texto |
| Estado | Selección (Por visitar / Visitado / Interesado / Cliente / Descartado) — empieza en "Por visitar" |
| Sector | Texto (Hostelería, Cita previa o el nicho libre) |
| Teléfono | Teléfono (E.164, con código de país) |
| Rating / Reseñas | Número |
| Ficha reclamada | Selección (Sí / No / Desconocido) |
| Tiene web | Selección (Sí / Solo redes o plataforma / No) |
| Categoría, Dirección, Municipio | Texto |
| Web, Google Maps | URL |
| Notas | Texto largo (para anotar las visitas) |
| Place ID | Texto (se usa para no duplicar con `--base-id`) |
| WhatsApp | Fórmula URL (vacía si no hay teléfono válido) |

### Fórmula del campo WhatsApp

```
IF({Teléfono}, "https://wa.me/" & SUBSTITUTE({Teléfono}, "+", "") & "?text=" & ENCODE_URL_COMPONENT("Hola, ¿hablo con " & {Empresa} & "?"))
```

El link es para **seguimiento** con negocios que ya dieron su contacto. En España, la LSSI (art. 21) limita las comunicaciones comerciales electrónicas no solicitadas, también entre empresas: el primer contacto recomendado es la visita en persona con el diagnóstico.

## Botón WhatsApp verde (paso manual opcional — 30 seg)

La API de Airtable no permite crear campos Button. Para un botón visual: **+** al final de las columnas → Tipo **Button** → Label `WhatsApp`, color verde → Action **Open URL** → pegar la fórmula de arriba → Save.

## Manejo de errores

| Error | Causa | Solución |
|---|---|---|
| Faltan variables de entorno | `APIFY_TOKEN`/`AIRTABLE_TOKEN` no configuradas | Agregarlas en la configuración del entorno |
| Más de 1000 lugares posibles | Corrida grande | Bajar `--max-per-search`, acotar zonas, o `--yes` tras confirmarlo con el usuario |
| Apify 401 | API key inválida | Verificar en apify.com |
| Apify FAILED / 0 resultados | Scraper falló o término sin resultados | Reintentar con nicho más genérico |
| Apify no terminó en N min | Run lento o colgado (ya abortado) | Bajar `--max-per-search` o subir `--max-wait` |
| Airtable 403 | Token sin permisos | Revisar scopes (ver arriba) |
| Tabla sin 'Place ID' | Base creada con una versión anterior | Crear una base nueva |
| workspace_id not found | Sin bases existentes para auto-detectar | Crear una base vacía en Airtable primero, o pasar `--workspace-id` |
| CONNECT tunnel failed 403 | Red del entorno bloquea el host | Permitir los dominios en la política de red del entorno |
