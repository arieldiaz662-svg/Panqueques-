#!/usr/bin/env python3
"""
Prospector: Scrape Google Maps leads via Apify → Airtable base con link WhatsApp
Usage:
  export APIFY_TOKEN=...  AIRTABLE_TOKEN=...
  python prospector.py --niche NICHE --zone ZONE
"""
import argparse
import os
import time
import sys
import requests
import phonenumbers

ACTOR_ID = "nwua9Gu5YrADL7ZDj"  # compass/crawler-google-places


# ─── Geocoding ────────────────────────────────────────────────────────────────

def geocode_zone(zone):
    """Devuelve (lat, lng, country_code) de la zona via Nominatim (OSM, sin key)."""
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
            lat = float(results[0]["lat"])
            lng = float(results[0]["lon"])
            cc = results[0].get("address", {}).get("country_code", "").upper()
            return lat, lng, cc
    except Exception as e:
        print(f"   ⚠️  Geocoding falló ({e}), se scrapea sin coordenadas")
    return None, None, None


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


def scrape_leads(api_key, niche, zone, max_results, max_wait):
    query = f"{niche} en {zone}"
    print(f"🔍 Scrapeando '{niche}' en '{zone}'...")

    lat, lng, country_code = geocode_zone(zone)
    if lat:
        print(f"   📍 {lat:.4f}, {lng:.4f} ({country_code})")
    else:
        print(f"   ⚠️  Sin coordenadas — los resultados podrían incluir otras zonas")

    payload = {
        "searchStringsArray": [query],
        "maxCrawledPlaces": max_results,
        "language": "es",
        "maxImages": 0,
        "maxReviews": 0,
        "includeHistogram": False,
        "includeOpeningHours": False,
        "includeWebResults": False,
    }
    if lat:
        payload["lat"] = lat
        payload["lng"] = lng
        payload["zoom"] = 12

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
                     f"Probá con --max-results más bajo o --max-wait más alto.")
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
        print(f"   Estado: {status} | Leads: {count}", end="\r", flush=True)

        if status == "SUCCEEDED":
            print()
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            print()
            sys.exit(f"❌ Apify falló: {status}")
        time.sleep(8)

    r = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
        params={"token": api_key},
        timeout=60,
    )
    r.raise_for_status()
    leads = r.json()

    if country_code:
        before = len(leads)
        # Solo descarta leads con país explícitamente distinto; los que vienen sin countryCode se conservan
        leads = [
            l for l in leads
            if not l.get("countryCode") or l["countryCode"].upper() == country_code
        ]
        removed = before - len(leads)
        if removed:
            print(f"   🗑️  {removed} leads de otros países descartados")

    print(f"✅ {len(leads)} leads de {zone}\n")
    return leads, country_code


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


def _base_fields():
    return [
        {"name": "Empresa",     "type": "singleLineText"},
        {"name": "Teléfono",    "type": "phoneNumber"},
        {"name": "Rating",      "type": "number",    "options": {"precision": 1}},
        {"name": "Reseñas",     "type": "number",    "options": {"precision": 0}},
        {"name": "Categoría",   "type": "singleLineText"},
        {"name": "Dirección",   "type": "singleLineText"},
        {"name": "Ciudad",      "type": "singleLineText"},
        {"name": "Web",         "type": "url"},
        {"name": "Google Maps", "type": "url"},
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


def upload_records(token, base_id, table_id, leads, default_region):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"

    records = []
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

        fields = {
            "Empresa":   (lead.get("title") or "").strip(),
            "Teléfono":  phone,
            "Categoría": cat,
            "Dirección": lead.get("address") or "",
            "Ciudad":    lead.get("city") or "",
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

        records.append({"fields": fields})

    if invalid_phones:
        print(f"   ⚠️  {invalid_phones} teléfonos no se pudieron normalizar (quedan sin link WhatsApp)")

    print(f"⬆️  Subiendo {len(records)} registros...")
    total = 0
    for i in range(0, len(records), 10):
        batch = records[i : i + 10]
        r = requests.post(url, headers=headers, json={"records": batch}, timeout=30)
        if r.status_code != 200:
            print(f"\n   ⚠️  Error en batch {i}: {r.status_code} — {r.text[:200]}")
            continue
        total += len(batch)
        print(f"   Subidos: {total}/{len(records)}", end="\r", flush=True)
        time.sleep(0.2)

    print()
    return total


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Scrape leads → Airtable con link WhatsApp. "
                    "Requiere las variables de entorno APIFY_TOKEN y AIRTABLE_TOKEN."
    )
    p.add_argument("--niche",           required=True,  help='Ej: "clínicas dentales"')
    p.add_argument("--zone",            required=True,  help='Ej: "Madrid"')
    p.add_argument("--max-results",     type=int, default=200)
    p.add_argument("--max-wait",        type=int, default=1800, help="Segundos máximos esperando a Apify (default 1800)")
    p.add_argument("--workspace-id",    default=None,   help="Airtable workspace ID (auto-detectado si se omite)")
    args = p.parse_args()

    apify_key = os.environ.get("APIFY_TOKEN")
    airtable_token = os.environ.get("AIRTABLE_TOKEN")
    missing = [n for n, v in (("APIFY_TOKEN", apify_key), ("AIRTABLE_TOKEN", airtable_token)) if not v]
    if missing:
        sys.exit(f"❌ Faltan variables de entorno: {', '.join(missing)}")

    base_name = f"Leads {args.niche.title()} {args.zone.title()}"

    leads, country_code = scrape_leads(apify_key, args.niche, args.zone, args.max_results, args.max_wait)
    if not leads:
        sys.exit("❌ Apify no devolvió resultados. Probá con un nicho o zona diferente.")

    workspace_id = args.workspace_id or get_workspace_id(airtable_token)
    print(f"   Workspace: {workspace_id}")

    base_id, table_id = create_base(airtable_token, base_name, workspace_id)

    uploaded = upload_records(airtable_token, base_id, table_id, leads, country_code)

    print(f"\n✅ Listo! {uploaded}/{len(leads)} leads en '{base_name}'")
    print(f"🔗 https://airtable.com/{base_id}")


if __name__ == "__main__":
    main()
