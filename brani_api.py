
import requests
import xml.etree.ElementTree as ET
import re
import json
import os
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# --- NASTAVENÍ PROMĚNNÝCH ---
BRANI_EMAIL = os.getenv("BRANI_EMAIL")
BRANI_HESLO = os.getenv("BRANI_HESLO")

# URL adresy pro tvé XML feedy (případně můžeš načítat z lokálního souboru)
ORDER_FEED_URL = os.getenv("ORDER_FEED_URL")
SUPPLIER_FEED_URL = os.getenv("SUPPLIER_FEED_URL")

# Soubor, kam si skript ukládá "paměť" o posledním spuštění
STATE_FILE = os.path.join(BASE_DIR, "sync_state.json")

# --- FUNKCE PRO PAMĚŤ SKRIPTU ---
def ziskej_posledni_synchronizaci():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('last_sync')
        except (json.JSONDecodeError, KeyError):
            return None
    return None

def uloz_aktualni_synchronizaci(cas_str, status="success"):
    data = {
        'last_sync': cas_str,
        'last_run_status': status,
        'updated_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# --- FUNKCE PRO BRANI A DODAVATELE ---
def ziskej_brani_token():
    print("1. Přihlašuji se do Brani...")
    response = requests.post(
        'https://auth.brani.cz/api/login/', 
        data={'username': BRANI_EMAIL, 'password': BRANI_HESLO}
    )
    if response.status_code == 200:
        return response.json().get('access_token')
    else:
        print(f"❌ Chyba přihlášení do Brani: {response.text}")
        return None

def zpracuj_dodavatelsky_feed(url):
    print("2. Stahuji a zpracovávám feed dodavatele Comad...")
    response = requests.get(url)
    response.raise_for_status() 
    
    root = ET.fromstring(response.content)
    baliky_mapa = {}
    
    for article in root.findall('.//Article'):
        ean = article.findtext('./EAN')
        if not ean:
            continue
            
        for attr in article.findall('./Attributes/Attribute'):
            if attr.findtext('./Code') == 'Ilość Paczek':
                pocet = attr.findtext('./Value')
                if pocet and pocet.isdigit():
                    baliky_mapa[ean] = int(pocet)
                break 
                
    print(f"   -> Do paměti načteno {len(baliky_mapa)} produktů s počtem balíků.")
    return baliky_mapa

def aktualizuj_brani_poznamku(token, eshop_id, order_code, nova_poznamka):
    url = 'https://balic.brani.cz/api/packing/set_remark/'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    payload = {
        "eshop_id": eshop_id,
        "is_multieshop_request": False,
        "order_code": str(order_code),
        "remark": nova_poznamka
    }
    
    # --- DRY RUN (Simulace) ---
    print(f"   🛠️ [SIMULACE] Cílová URL: {url}")
    print(f"   🛠️ [SIMULACE] Odesílaná data (Payload):")
    print(json.dumps(payload, indent=4, ensure_ascii=False))
    print("   -------------------------------------------------")
    
    # ZDE ODSTRANÍŠ MŘÍŽKY, AŽ TO BUDEŠ CHTÍT POSÍLAT NA OSTRO:
    # response = requests.post(url, headers=headers, json=payload)
    # if response.status_code in [200, 201]:
    #     print(f"   ✅ Poznámka uložena (Objednávka: {order_code}, E-shop: {eshop_id})")
    # else:
    #     print(f"   ❌ Chyba ukládání ({order_code}): {response.status_code} - {response.text}")

# --- HLAVNÍ LOGIKA PRO OBJEDNÁVKY ---
def zpracuj_objednavky(token, baliky_mapa, base_feed_url, last_sync_str):
    # 1. Úprava URL adresy feedu na základě poslední synchronizace
    if last_sync_str:
        # Nahradíme mezeru znakem %20 pro platnou URL adresu
        url_time = last_sync_str.replace(" ", "%20")
        # Rozhodneme se, zda přidat ? nebo &, podle toho, jak vypadá základní URL
        separator = "&" if "?" in base_feed_url else "?"
        feed_url = f"{base_feed_url}{separator}updateTimeFrom={url_time}"
        print(f"\n3. Stahuji objednávky z: {feed_url}")
    else:
        feed_url = base_feed_url
        print("\n3. První spuštění: Stahuji všechny dostupné objednávky z feedu...")

    # Zpracování času poslední synchronizace do formátu datetime (pro porovnávání <DATE>)
    last_sync_dt = datetime.strptime(last_sync_str, "%Y-%m-%d %H:%M:%S") if last_sync_str else None

    # Stažení a vyčištění XML
    response = requests.get(feed_url)
    response.raise_for_status()
    xml_text = response.content.decode('utf-8-sig').strip()
    xml_text = re.sub(r'<\?xml.*?\?>', '', xml_text).strip()
    if not xml_text.startswith('<ORDERS>'):
        xml_text = f"<ORDERS>{xml_text}</ORDERS>"
        
    root = ET.fromstring(xml_text)
    
    zpracovano = 0
    preskoceno = 0

    for order in root.findall('.//ORDER'):
        code = order.findtext('CODE')
        shop_remark = order.findtext('SHOP_REMARK') or ""
        order_date_str = order.findtext('DATE')
        
        # 💡 KONTROLA 1: Byly balíky už zapsány? (Ochrana proti duplicitám)
        if "🔴POČET BALÍKŮ:" in shop_remark:
            preskoceno += 1
            continue

        # 💡 KONTROLA 2: Filtrace starých objednávek (pokud nám feed poslal aktualizovanou)
        if last_sync_dt and order_date_str:
            order_dt = datetime.strptime(order_date_str, "%Y-%m-%d %H:%M:%S")
            if order_dt <= last_sync_dt:
                preskoceno += 1
                continue

        celkovy_pocet_baliku = 0
        comad_nalezen = False
        
        for item in order.findall('.//ITEM'):
            item_type = item.findtext('TYPE')
            if item_type == 'product':
                manufacturer = (item.findtext('MANUFACTURER') or "").lower()
                supplier = (item.findtext('SUPPLIER') or "").lower()
                
                if 'comad' in manufacturer or 'comad' in supplier:
                    comad_nalezen = True
                    ean = item.findtext('EAN')
                    
                    if ean in baliky_mapa:
                        mnozstvi = int(float(item.findtext('AMOUNT') or 1))
                        celkovy_pocet_baliku += (baliky_mapa[ean] * mnozstvi)
        
        if not comad_nalezen:
            continue
            
        print(f"\nZpracovávám novou objednávku: {code} (Datum: {order_date_str}, Balíků: {celkovy_pocet_baliku})")
        zpracovano += 1

        eshop_id = 4256
        order_code = code
        base_match = re.search(r"Base\.com Order ID:\s*(\d+)", shop_remark)
        
        if base_match:
            order_code = base_match.group(1)
            eshop_id = 6038
            
        shop_remark = shop_remark.strip()
        vysledna_poznamka = f"{shop_remark}\n🔴POČET BALÍKŮ: {celkovy_pocet_baliku}🔴"
        
        aktualizuj_brani_poznamku(token, eshop_id, order_code, vysledna_poznamka)

    print(f"\nShrnutí: {zpracovano} objednávek zpracováno, {preskoceno} přeskočeno (staré nebo již obsahují poznámku).")

# --- HLAVNÍ SPUŠTĚNÍ SKRIPTU ---
if __name__ == "__main__":
    # 1. Zaznamenáme aktuální čas pro příští synchronizaci
    current_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 2. Zjistíme, zda už máme nějakou historii
    last_sync = ziskej_posledni_synchronizaci()
    
    if last_sync is None:
        # --- SCÉNÁŘ: PRVNÍ SPUŠTĚNÍ (INICIALIZACE) ---
        print("--- PRVNÍ SPUŠTĚNÍ: Inicializace systému ---")
        uloz_aktualni_synchronizaci(current_run_time, status="initialized")
        print(f"✅ Čas {current_run_time} byl uložen jako výchozí bod.")
        print("💡 Žádné objednávky nebyly staženy ani zpracovány. Skript končí.")
        # Skript zde skončí a nebude pokračovat k přihlašování a feedům
    else:
        # --- SCÉNÁŘ: BĚŽNÁ SYNCHRONIZACE ---
        print(f"--- Skript spuštěn. Zpracovávám nové změny od: {last_sync} ---")
        
        access_token = ziskej_brani_token()
        
        if access_token:
            try:
                # Načtení dat od dodavatele
                mapa_baliku = zpracuj_dodavatelsky_feed(SUPPLIER_FEED_URL)
                
                # Zpracování objednávek (včetně parametru updateTimeFrom a kontroly <DATE>)
                zpracuj_objednavky(access_token, mapa_baliku, ORDER_FEED_URL, last_sync)
                
                # Pokud vše proběhlo v pořádku, uložíme čas tohoto spuštění pro příště
                uloz_aktualni_synchronizaci(current_run_time, status="success")
                print(f"\n🎉 Synchronizace hotova. Příště budeme pokračovat od: {current_run_time}")
                
            except Exception as e:
                print(f"❌ Došlo k nečekané chybě: {e}")
                print("⚠️ Čas synchronizace NEBYL aktualizován, aby nedošlo ke ztrátě dat.")