#!/usr/bin/env python3
"""
Prospector: Scrape Google Maps leads via Apify → Airtable base calificada para prospección
Usage:
  export APIFY_TOKEN=...  AIRTABLE_TOKEN=...
  python prospector.py --sector hosteleria --zone-preset tenerife
  python prospector.py --niche "clínicas dentales" --zone "Madrid"
"""
import argparse
import os
import re
import time
import sys
from urllib.parse import urlparse

import requests
import phonenumbers

ACTOR_ID = "nwua9Gu5YrADL7ZDj"  # compass/crawler-google-places

# Por encima de este número máximo de lugares hay que confirmar con --yes (consume créditos de Apify)
CONFIRM_THRESHOLD = 1000


# ─── Presets ──────────────────────────────────────────────────────────────────

SECTORS = {
    "hosteleria": {
        "label": "Hostelería",
        "niches": ["bares", "restaurantes", "cafeterías"],
    },
    "cita-previa": {
        "label": "Cita previa",
        "niches": ["barberías", "peluquerías", "centros de estética", "salones de uñas"],
    },
}

ZONE_PRESETS = {
    "tenerife": {
        "label": "Tenerife",
        "country": "ES",
        # Tenerife usa los códigos postales 380xx–386xx (387xx–389xx son La Palma, La Gomera y El Hierro)
        "postal": r"^38[0-6]\d\d$",
        "zones": [f"{m}, Tenerife" for m in [
            "Adeje", "Arafo", "Arico", "Arona", "Buenavista del Norte", "Candelaria",
            "El Rosario", "El Sauzal", "El Tanque", "Fasnia", "Garachico", "Granadilla de Abona",
            "Guía de Isora", "Güímar", "Icod de los Vinos", "La Guancha", "La Matanza de Acentejo",
            "La Orotava", "La Victoria de Acentejo", "Los Realejos", "Los Silos", "Puerto de la Cruz",
            "San Cristóbal de La Laguna", "San Juan de la Rambla", "San Miguel de Abona",
            "Santa Cruz de Tenerife", "Santa Úrsula", "Santiago del Teide", "Tacoronte",
            "Tegueste", "Vilaflor de Chasna",
        ]],
    },
}

# Dominios que no cuentan como web propia: redes sociales y plataformas de reservas
SOCIAL_DOMAINS = (
    "instagram.com", "facebook.com", "fb.com", "tiktok.com", "linktr.ee", "wa.me", "whatsapp.com",
    "twitter.com", "x.com", "youtube.com", "booksy.com", "treatwell.es", "treatwell.com", "fresha.com",
    "thefork.es", "thefork.com", "eltenedor.es", "tripadvisor.es", "tripadvisor.com", "just-eat.es",
    "glovoapp.com", "ubereats.com", "google.com", "business.site", "linkin.bio", "beacons.ai",
)


# ─── Geocoding ────────────────────────────────────────────────────────────────

def geocode_country(zone):
    """Devuelve el country_code ISO de la zona via Nominatim (OSM, sin key), o None."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": zone, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "prospector-skill/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            return results[0].get("address", {}).get("country_code", "").upper() or None
    except Exception as e:
        print(f"   ⚠️  Geocoding falló ({e}), no se filtra por país")
    return None


# ─── Apify ────────────────────────────────────────────────────────────────────

def abort_run(api_key, run_id):
    try:
        requests.post(
            f"https://api.apify.com/v2/actor-runs/{run_id}/abort",
            params={"token": api_key},
            timeout=15,
        )
    except requests.RequestException:
        pass


def run_apify(api_key, queries, max_per_search, country_code, max_wait):
    payload = {
        "searchStringsArray": queries,
        "maxCrawledPlacesPerSearch": max_per_search,
        "language": "es",
        "maxImages": 0,
        "maxReviews": 0,
        "includeHistogram": False,
        "includeOpeningHours": False,
        "includeWebResults": False,
    }
    if country_code:
        payload["countryCode"] = country_code.lower()

    r = requests.post(
        f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs",
        params={"token": api_key},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    run_id = r.json()["data"]["id"]
    print(f"   Run: {run_id}")

    deadline = time.monotonic() + max_wait
    errors = 0
    while True:
        if time.monotonic() > deadline:
            print()
            abort_run(api_key, run_id)
            sys.exit(f"❌ Apify no terminó en {max_wait // 60} min — run {run_id} abortado. "
                     f"Probá con --max-per-search más bajo o --max-wait más alto.")
        try:
            r = requests.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}",
                params={"token": api_key},
                timeout=15,
            )
            r.raise_for_status()
            errors = 0
        except requests.RequestException as e:
            errors += 1
            if errors >= 5:
                print()
                sys.exit(f"❌ No se pudo consultar el run {run_id} tras 5 intentos: {e}")
            time.sleep(8)
            continue
        data = r.json()["data"]
        status = data["status"]
        count = data.get("stats", {}).get("itemCount", 0)
        print(f"   Estado: {status} | Lugares: {count}", end="\r", flush=True)

        if status == "SUCCEEDED":
            print()
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            print()
            sys.exit(f"❌ Apify falló: {status}")
        time.sleep(8)

    r = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
        params={"token": api_key, "clean": "true"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


# ─── Filtrado y calificación ──────────────────────────────────────────────────

def lead_key(lead):
    return lead.get("placeId") or f"{(lead.get('title') or '').lower()}|{(lead.get('address') or '').lower()}"


def postal_code(lead):
    if lead.get("postalCode"):
        return str(lead["postalCode"]).strip()
    m = re.search(r"\b(\d{5})\b", lead.get("address") or "")
    return m.group(1) if m else ""


def filter_leads(leads, country_code, postal_re):
    """Descarta duplicados, cerrados definitivamente, otro país y fuera de la zona postal."""
    seen = set()
    kept = []
    removed = {"duplicados": 0, "cerrados": 0, "otro país": 0, "fuera de zona": 0}
    for lead in leads:
        key = lead_key(lead)
        if key in seen:
            removed["duplicados"] += 1
            continue
        seen.add(key)
        if lead.get("permanentlyClosed"):
            removed["cerrados"] += 1
            continue
        # Solo descarta países explícitamente distintos; los que vienen sin countryCode se conservan
        if country_code and lead.get("countryCode") and lead["countryCode"].upper() != country_code:
            removed["otro país"] += 1
            continue
        if postal_re:
            cp = postal_code(lead)
            if cp and not re.match(postal_re, cp):
                removed["fuera de zona"] += 1
                continue
        kept.append(lead)
    for reason, n in removed.items():
        if n:
            print(f"   🗑️  {n} descartados ({reason})")
    return kept


def web_status(url):
    if not url:
        return "No"
    host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    if any(host == d or host.endswith(f".{d}") for d in SOCIAL_DOMAINS):
        return "Solo redes o plataforma"
    return "Sí"


def qualify(lead):
    """Devuelve (puntuación, prioridad, diagnóstico, estado web, ficha reclamada).

    La puntuación refleja cuánto le falta al negocio de lo que vende una agencia de
    presencia digital local: web propia, ficha de Google reclamada y reseñas.
    """
    score = 0
    notes = []

    web = web_status(lead.get("website"))
    if web == "No":
        score += 3
        notes.append("sin web")
    elif web == "Solo redes o plataforma":
        score += 2
        notes.append("solo redes/plataforma, sin web propia")

    claim = lead.get("claimThisBusiness")
    if claim is True:
        score += 2
        claimed = "No"
        notes.append("ficha sin reclamar")
    elif claim is False:
        claimed = "Sí"
    else:
        claimed = "Desconocido"

    reviews = lead.get("reviewsCount")
    try:
        reviews = int(reviews) if reviews is not None else None
    except (TypeError, ValueError):
        reviews = None
    if reviews is None or reviews < 30:
        score += 2
        notes.append("sin reseñas" if not reviews else f"{reviews} reseñas")
    elif reviews < 100:
        score += 1
        notes.append(f"{reviews} reseñas")

    rating = lead.get("totalScore")
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None
    if rating and rating < 4.2:
        score += 1
        notes.append(f"rating {rating:.1f}".replace(".", ","))

    if lead.get("temporarilyClosed"):
        notes.append("cerrado temporalmente")

    priority = "Alta" if score >= 5 else "Media" if score >= 3 else "Baja"
    diagnosis = (" · ".join(notes) or "presencia digital completa").capitalize()
    return score, priority, diagnosis, web, claimed


def normalize_phone(raw, region):
    """Devuelve el teléfono en formato E.164 (+CCNNNN...) o "" si no es válido.

    Si el número viene en formato local, se usa `region` (código ISO del país)
    para agregar el prefijo internacional.
    """
    if not raw:
        return ""
    try:
        num = phonenumbers.parse(raw, region or None)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(num):
        return ""
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def build_records(leads, query_sector, default_region):
    rows = []
    invalid_phones = 0
    for lead in leads:
        cat = lead.get("categoryName") or lead.get("category") or ""
        if not cat and lead.get("categories"):
            cat = lead["categories"][0] if isinstance(lead["categories"], list) else str(lead["categories"])

        phone_raw = lead.get("phoneUnformatted") or lead.get("phone") or ""
        region = (lead.get("countryCode") or default_region or "").upper()
        phone = normalize_phone(phone_raw, region)
        if phone_raw and not phone:
            invalid_phones += 1

        score, priority, diagnosis, web, claimed = qualify(lead)

        fields = {
            "Empresa":         (lead.get("title") or "").strip(),
            "Prioridad":       priority,
            "Puntuación":      score,
            "Diagnóstico":     diagnosis,
            "Estado":          "Por visitar",
            "Sector":          query_sector.get(lead.get("searchString") or "", ""),
            "Teléfono":        phone,
            "Ficha reclamada": claimed,
            "Tiene web":       web,
            "Categoría":       cat,
            "Dirección":       lead.get("address") or "",
            "Municipio":       lead.get("city") or "",
            "Place ID":        lead.get("placeId") or "",
        }
        if lead.get("totalScore") is not None:
            try:
                fields["Rating"] = round(float(lead["totalScore"]), 1)
            except (TypeError, ValueError):
                pass
        if lead.get("reviewsCount") is not None:
            try:
                fields["Reseñas"] = int(lead["reviewsCount"])
            except (TypeError, ValueError):
                pass
        if lead.get("website"):
            fields["Web"] = lead["website"]
        if lead.get("url"):
            fields["Google Maps"] = lead["url"]

        rows.append({"fields": fields})

    if invalid_phones:
        print(f"   ⚠️  {invalid_phones} teléfonos no se pudieron normalizar (quedan sin link WhatsApp)")
    # Los de mayor prioridad primero, para que aparezcan arriba en la vista por defecto
    rows.sort(key=lambda r: -r["fields"]["Puntuación"])
    return rows


# ─── Airtable ─────────────────────────────────────────────────────────────────

def get_workspace_id(token):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get("https://api.airtable.com/v0/meta/bases", headers=headers, timeout=15)
    if r.status_code == 200:
        bases = r.json().get("bases", [])
        for base in bases:
            base_id = base.get("id")
            if not base_id:
                continue
            r2 = requests.get(f"https://api.airtable.com/v0/meta/bases/{base_id}", headers=headers, timeout=15)
            if r2.status_code == 200:
                wid = r2.json().get("workspaceId")
                if wid:
                    return wid
    raise SystemExit(
        "❌ No se pudo detectar workspace ID automáticamente.\n"
        "   Solución: abre airtable.com, entrá a tu workspace, mirá la URL\n"
        "   (formato: wspXXXXXXXX) y agregá --workspace-id 'wspXXXXXXXX' al comando."
    )


WA_FORMULA = (
    'IF({Teléfono}, "https://wa.me/" & SUBSTITUTE({Teléfono}, "+", "") '
    '& "?text=" & ENCODE_URL_COMPONENT("Hola, ¿hablo con " & {Empresa} & "?"))'
)


def _select(*choices):
    return {"choices": [{"name": n, "color": c} for n, c in choices]}


def _base_fields():
    return [
        {"name": "Empresa",         "type": "singleLineText"},
        {"name": "Prioridad",       "type": "singleSelect", "options": _select(
            ("Alta", "redBright"), ("Media", "yellowBright"), ("Baja", "grayBright"))},
        {"name": "Puntuación",      "type": "number", "options": {"precision": 0}},
        {"name": "Diagnóstico",     "type": "singleLineText"},
        {"name": "Estado",          "type": "singleSelect", "options": _select(
            ("Por visitar", "blueBright"), ("Visitado", "cyanBright"), ("Interesado", "yellowBright"),
            ("Cliente", "greenBright"), ("Descartado", "grayBright"))},
        {"name": "Sector",          "type": "singleLineText"},
        {"name": "Teléfono",        "type": "phoneNumber"},
        {"name": "Rating",          "type": "number", "options": {"precision": 1}},
        {"name": "Reseñas",         "type": "number", "options": {"precision": 0}},
        {"name": "Ficha reclamada", "type": "singleSelect", "options": _select(
            ("Sí", "greenBright"), ("No", "redBright"), ("Desconocido", "grayBright"))},
        {"name": "Tiene web",       "type": "singleSelect", "options": _select(
            ("Sí", "greenBright"), ("Solo redes o plataforma", "yellowBright"), ("No", "redBright"))},
        {"name": "Categoría",       "type": "singleLineText"},
        {"name": "Dirección",       "type": "singleLineText"},
        {"name": "Municipio",       "type": "singleLineText"},
        {"name": "Web",             "type": "url"},
        {"name": "Google Maps",     "type": "url"},
        {"name": "Notas",           "type": "multilineText"},
        {"name": "Place ID",        "type": "singleLineText"},
    ]


def create_base(token, name, workspace_id):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"📋 Creando base '{name}'...")

    payload = {
        "name": name,
        "workspaceId": workspace_id,
        "tables": [{"name": "Leads", "fields": _base_fields()}],
    }
    r = requests.post("https://api.airtable.com/v0/meta/bases", headers=headers, json=payload, timeout=30)
    r.raise_for_status()

    data = r.json()
    base_id = data["id"]
    table_id = data["tables"][0]["id"]
    print(f"   Base ID: {base_id}")

    # La API de Airtable no permite crear campos Button: se crea como fórmula URL
    print(f"   Agregando campo WhatsApp...")
    field_url = f"https://api.airtable.com/v0/meta/bases/{base_id}/tables/{table_id}/fields"
    r = requests.post(field_url, headers=headers, json={
        "name": "WhatsApp",
        "type": "formula",
        "options": {"formula": WA_FORMULA},
    }, timeout=15)
    if r.status_code not in (200, 201):
        print(f"   ⚠️  Campo WhatsApp no se pudo agregar: {r.text[:200]}")
    else:
        print(f"   ✅ Campo fórmula URL agregado (clickeable)")

    print()
    return base_id, table_id


def find_leads_table(token, base_id):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"https://api.airtable.com/v0/meta/bases/{base_id}/tables", headers=headers, timeout=15)
    r.raise_for_status()
    for table in r.json().get("tables", []):
        if table["name"] == "Leads":
            if not any(f["name"] == "Place ID" for f in table.get("fields", [])):
                sys.exit("❌ La tabla 'Leads' de esa base no tiene el campo 'Place ID' "
                         "(¿fue creada con una versión anterior del skill?). Creá una base nueva.")
            return table["id"]
    sys.exit(f"❌ La base {base_id} no tiene una tabla llamada 'Leads'.")


def existing_place_ids(token, base_id, table_id):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    ids = set()
    offset = None
    while True:
        params = {"fields[]": "Place ID", "pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for rec in data.get("records", []):
            pid = rec.get("fields", {}).get("Place ID")
            if pid:
                ids.add(pid)
        offset = data.get("offset")
        if not offset:
            return ids
        time.sleep(0.2)


def upload_records(token, base_id, table_id, records):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"

    print(f"⬆️  Subiendo {len(records)} registros...")
    total = 0
    for i in range(0, len(records), 10):
        batch = records[i : i + 10]
        r = requests.post(url, headers=headers, json={"records": batch, "typecast": True}, timeout=30)
        if r.status_code != 200:
            print(f"\n   ⚠️  Error en batch {i}: {r.status_code} — {r.text[:200]}")
            continue
        total += len(batch)
        print(f"   Subidos: {total}/{len(records)}", end="\r", flush=True)
        time.sleep(0.2)

    print()
    return total


# ─── Main ─────────────────────────────────────────────────────────────────────

def build_plan(args):
    """Devuelve (queries, query→sector, zona_label, country, postal_re, sector_label)."""
    niches = []  # (nicho, etiqueta de sector)
    for s in args.sector or []:
        niches += [(n, SECTORS[s]["label"]) for n in SECTORS[s]["niches"]]
    niches += [(n, n.capitalize()) for n in args.niche or []]
    if not niches:
        sys.exit("❌ Indicá al menos un --sector o --niche.")

    zones = []
    zone_label = None
    country = args.country.upper() if args.country else None
    postal_re = args.postal_regex
    if args.zone_preset:
        preset = ZONE_PRESETS[args.zone_preset]
        zones += preset["zones"]
        zone_label = preset["label"]
        country = country or preset["country"]
        postal_re = postal_re or preset.get("postal")
    zones += args.zone or []
    if not zones:
        sys.exit("❌ Indicá al menos una --zone o un --zone-preset.")
    if not zone_label:
        zone_label = zones[0] if len(zones) == 1 else f"{zones[0]} y {len(zones) - 1} zonas más"

    queries = []
    query_sector = {}
    for niche, sector in niches:
        for zone in zones:
            q = f"{niche} en {zone}"
            if q not in query_sector:
                queries.append(q)
                query_sector[q] = sector

    sector_labels = list(dict.fromkeys(s for _, s in niches))
    sector_label = sector_labels[0] if len(sector_labels) == 1 else " + ".join(sector_labels)
    return queries, query_sector, zone_label, country, postal_re, sector_label


def main():
    p = argparse.ArgumentParser(
        description="Scrape leads de Google Maps → Airtable con calificación y link WhatsApp. "
                    "Requiere las variables de entorno APIFY_TOKEN y AIRTABLE_TOKEN."
    )
    p.add_argument("--sector",         action="append", choices=sorted(SECTORS),
                   help="Grupo de nichos predefinido (repetible)")
    p.add_argument("--niche",          action="append", help='Nicho libre, ej: "clínicas dentales" (repetible)')
    p.add_argument("--zone",           action="append", help='Zona, ej: "Adeje, Tenerife" (repetible)')
    p.add_argument("--zone-preset",    choices=sorted(ZONE_PRESETS), help="Lista de municipios predefinida")
    p.add_argument("--country",        help="Código ISO del país (si no, se deduce del preset o de la zona)")
    p.add_argument("--postal-regex",   help=r"Solo conserva códigos postales que cumplan este regex, ej: '^38[0-6]\d\d$'")
    p.add_argument("--max-per-search", type=int, default=50, help="Máximo de lugares por búsqueda (nicho × zona)")
    p.add_argument("--max-wait",       type=int, default=3600, help="Segundos máximos esperando a Apify (default 3600)")
    p.add_argument("--workspace-id",   default=None, help="Airtable workspace ID (auto-detectado si se omite)")
    p.add_argument("--base-id",        default=None, help="Agregar a una base existente (sin duplicar) en vez de crear otra")
    p.add_argument("--dry-run",        action="store_true", help="Mostrar las búsquedas y el máximo de lugares, sin ejecutar")
    p.add_argument("--yes",            action="store_true", help=f"Confirmar corridas de más de {CONFIRM_THRESHOLD} lugares")
    args = p.parse_args()

    queries, query_sector, zone_label, country, postal_re, sector_label = build_plan(args)
    max_places = len(queries) * args.max_per_search

    print(f"🔍 {len(queries)} búsquedas · hasta {args.max_per_search} lugares cada una "
          f"→ máximo {max_places} lugares (lo que se cobra en Apify)")
    if args.dry_run:
        for q in queries:
            print(f"   - {q}")
        return
    if max_places > CONFIRM_THRESHOLD and not args.yes:
        sys.exit(f"❌ Más de {CONFIRM_THRESHOLD} lugares posibles. Bajá --max-per-search, "
                 f"acotá zonas/nichos o confirmá con --yes.")

    apify_key = os.environ.get("APIFY_TOKEN")
    airtable_token = os.environ.get("AIRTABLE_TOKEN")
    missing = [n for n, v in (("APIFY_TOKEN", apify_key), ("AIRTABLE_TOKEN", airtable_token)) if not v]
    if missing:
        sys.exit(f"❌ Faltan variables de entorno: {', '.join(missing)}")

    if not country:
        country = geocode_country(args.zone[0])
    if country:
        print(f"   🌍 País: {country}")

    leads = run_apify(apify_key, queries, args.max_per_search, country, args.max_wait)
    print(f"   {len(leads)} lugares encontrados")
    leads = filter_leads(leads, country, postal_re)
    if not leads:
        sys.exit("❌ No quedaron leads. Probá con otro nicho o zona.")

    if args.base_id:
        base_id = args.base_id
        table_id = find_leads_table(airtable_token, base_id)
        known = existing_place_ids(airtable_token, base_id, table_id)
        before = len(leads)
        leads = [l for l in leads if not l.get("placeId") or l["placeId"] not in known]
        print(f"   ♻️  {before - len(leads)} ya estaban en la base")
        base_name = base_id
    else:
        base_name = f"Leads {sector_label} {zone_label}"
        workspace_id = args.workspace_id or get_workspace_id(airtable_token)
        print(f"   Workspace: {workspace_id}")
        base_id, table_id = create_base(airtable_token, base_name, workspace_id)

    records = build_records(leads, query_sector, country)
    counts = {k: sum(1 for r in records if r["fields"]["Prioridad"] == k) for k in ("Alta", "Media", "Baja")}
    print(f"✅ {len(records)} leads · Alta: {counts['Alta']} · Media: {counts['Media']} · Baja: {counts['Baja']}\n")

    uploaded = upload_records(airtable_token, base_id, table_id, records) if records else 0

    print(f"\n✅ Listo! {uploaded}/{len(records)} leads en '{base_name}'")
    print(f"🔗 https://airtable.com/{base_id}")


if __name__ == "__main__":
    main()
