
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
COMAD_FEED_URL = os.getenv("COMAD_FEED_URL")
ELTAP_FEED_URL = os.getenv("ELTAP_FEED_URL")

# Soubor, kam si skript ukládá "paměť" o posledním spuštění
STATE_FILE = os.path.join(BASE_DIR, "sync_state.json")
COMAD_LOCAL_FILE = os.path.join(BASE_DIR, "feedy", "comad_feed.xml")
ELTAP_LOCAL_FILE = os.path.join(BASE_DIR, "feedy", "eltap_feed.xml")

# --- FUNKCE PRO PAMĚŤ A STAV (JSON) ---
def nacti_stav():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}

def uloz_stav(stav_dict):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(stav_dict, f, indent=4, ensure_ascii=False)

# --- FUNKCE PRO STAHOVÁNÍ A ČTENÍ DODAVATELŮ (1x DENNĚ) ---
def zajisti_dodavatelske_feedy(stav):
    dnesni_datum = datetime.now().strftime("%Y-%m-%d")
    posledni_stazeni = stav.get('last_supplier_update')
    
    # Podmínka: Stáhneme feedy, pokud dnes ještě nebyly staženy, NEBO pokud lokální soubory fyzicky chybí
    potreba_stahnout = (posledni_stazeni != dnesni_datum) or not os.path.exists(COMAD_LOCAL_FILE) or not os.path.exists(ELTAP_LOCAL_FILE)
    
    if potreba_stahnout:
        print(f"2. Stahuji aktuální dodavatelské feedy (Nové pro dnešní den: {dnesni_datum})...")
        
        # Stáhnout a uložit Comad
        print("   -> Stahuji Comad...")
        resp_comad = requests.get(COMAD_FEED_URL)
        resp_comad.raise_for_status()
        with open(COMAD_LOCAL_FILE, 'wb') as f:
            f.write(resp_comad.content)
            
        # Stáhnout a uložit Eltap
        print("   -> Stahuji Eltap...")
        resp_eltap = requests.get(ELTAP_FEED_URL)
        resp_eltap.raise_for_status()
        with open(ELTAP_LOCAL_FILE, 'wb') as f:
            f.write(resp_eltap.content)
            
        # Aktualizovat datum v paměti
        stav['last_supplier_update'] = dnesni_datum
        uloz_stav(stav)
        print("   ✅ Feedy uloženy lokálně pro dnešní den.")
    else:
        print(f"2. Dodavatelské feedy už byly dnes ({dnesni_datum}) staženy. Načítám z lokálního disku...")

def zpracuj_comad_feed():
    # Načítáme z lokálního souboru, ne z internetu
    tree = ET.parse(COMAD_LOCAL_FILE)
    root = tree.getroot()
    baliky_mapa = {}
    
    for article in root.findall('.//Article'):
        ean = article.findtext('./EAN')
        if not ean: continue
            
        for attr in article.findall('./Attributes/Attribute'):
            if attr.findtext('./Code') == 'Ilość Paczek':
                pocet = attr.findtext('./Value')
                if pocet and pocet.isdigit():
                    baliky_mapa[ean] = int(pocet)
                break 
                
    print(f"   -> [COMAD] Načteno {len(baliky_mapa)} produktů.")
    return baliky_mapa

def zpracuj_eltap_feed():
    tree = ET.parse(ELTAP_LOCAL_FILE)
    root = tree.getroot()
    baliky_mapa = {}
    
    for product in root.findall('.//Product'):
        ean = product.findtext('./EAN')
        pocet = product.findtext('./Ilosc_paczek')
        
        if ean and pocet and pocet.isdigit():
            baliky_mapa[ean] = int(pocet)
            
    print(f"   -> [ELTAP] Načteno {len(baliky_mapa)} produktů.")
    return baliky_mapa

# --- FUNKCE PRO BRANI ---
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
    
    # --- DRY RUN (Simulace) - Odkryj request.post až to budeš chtít na ostro ---
    print(f"   🛠️ [SIMULACE] Payload pro Brani:\n{json.dumps(payload, indent=4, ensure_ascii=False)}")
    print("   -------------------------------------------------")
    
    # response = requests.post(url, headers=headers, json=payload)
    # if response.status_code in [200, 201]:
    #     print(f"   ✅ Uloženo (Obj: {order_code})")
    # else:
    #     print(f"   ❌ Chyba ukládání ({order_code}): {response.text}")

# --- HLAVNÍ LOGIKA PRO OBJEDNÁVKY ---
def zpracuj_objednavky(token, mapa_comad, mapa_eltap, base_feed_url, stav):
    last_sync_str = stav.get('last_sync')
    
    if last_sync_str:
        url_time = last_sync_str.replace(" ", "%20")
        separator = "&" if "?" in base_feed_url else "?"
        feed_url = f"{base_feed_url}{separator}updateTimeFrom={url_time}"
        print(f"\n3. Stahuji objednávky z: {feed_url}")
    else:
        feed_url = base_feed_url
        print("\n3. První spuštění: Stahuji všechny dostupné objednávky z feedu...")

    last_sync_dt = datetime.strptime(last_sync_str, "%Y-%m-%d %H:%M:%S") if last_sync_str else None

    # Ošetření XML
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
        
        # Ochrana proti přepisování už zapsaných balíků
        if "🔴COMAD BALÍKY:" in shop_remark or "🔴ELTAP BALÍKY:" in shop_remark:
            preskoceno += 1
            continue

        # Časová filtrace aktualizovaných (ale starých) objednávek
        if last_sync_dt and order_date_str:
            order_dt = datetime.strptime(order_date_str, "%Y-%m-%d %H:%M:%S")
            if order_dt <= last_sync_dt:
                preskoceno += 1
                continue

        comad_baliky = 0
        eltap_baliky = 0
        
        for item in order.findall('.//ITEM'):
            item_type = item.findtext('TYPE')
            if item_type == 'product':
                manufacturer = (item.findtext('MANUFACTURER') or "").lower()
                supplier = (item.findtext('SUPPLIER') or "").lower()
                ean = item.findtext('EAN')
                mnozstvi = int(float(item.findtext('AMOUNT') or 1))
                
                # Zpracování COMAD
                if 'comad' in manufacturer or 'comad' in supplier:
                    if ean in mapa_comad:
                        comad_baliky += (mapa_comad[ean] * mnozstvi)
                        
                # Zpracování ELTAP
                elif 'eltap' in manufacturer or 'eltap' in supplier:
                    if ean in mapa_eltap:
                        eltap_baliky += (mapa_eltap[ean] * mnozstvi)
        
        # Pokud v objednávce není nic od Comad ani Eltap, přeskočíme ji
        if comad_baliky == 0 and eltap_baliky == 0:
            continue
            
        print(f"\nZpracovávám: {code} (Comad balíků: {comad_baliky}, Eltap balíků: {eltap_baliky})")
        zpracovano += 1

        eshop_id = 4256
        order_code = code
        base_match = re.search(r"Base\.com Order ID:\s*(\d+)", shop_remark)
        if base_match:
            order_code = base_match.group(1)
            eshop_id = 6038
            
        # Sestavení nové poznámky s podmínkami
        pridavek_k_poznamce = ""
        if comad_baliky > 0:
            pridavek_k_poznamce += f"\n🔴COMAD BALÍKY: {comad_baliky}🔴"
        if eltap_baliky > 0:
            pridavek_k_poznamce += f"\n🔴ELTAP BALÍKY: {eltap_baliky}🔴"
            
        vysledna_poznamka = f"{shop_remark.strip()}{pridavek_k_poznamce}"
        
        aktualizuj_brani_poznamku(token, eshop_id, order_code, vysledna_poznamka)

    print(f"\nShrnutí: {zpracovano} objednávek odesláno, {preskoceno} přeskočeno.")

# --- HLAVNÍ SPUŠTĚNÍ ---
if __name__ == "__main__":
    current_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stav = nacti_stav()
    
    # Scénář A: První spuštění (jen inicializace)
    if 'last_sync' not in stav:
        print("--- PRVNÍ SPUŠTĚNÍ: Inicializace systému ---")
        stav['last_sync'] = current_run_time
        stav['last_run_status'] = 'initialized'
        uloz_stav(stav)
        print(f"✅ Čas {current_run_time} byl uložen. Skript končí bez zpracování objednávek.")
    
    # Scénář B: Běžná synchronizace
    else:
        print(f"--- Skript spuštěn. Zpracovávám nové změny od: {stav['last_sync']} ---")
        
        access_token = ziskej_brani_token()
        if access_token:
            try:
                # 1. Zkontroluje, stáhne (pokud je potřeba) a uloží feedy
                zajisti_dodavatelske_feedy(stav)
                
                # 2. Načte data z lokálních souborů (velmi rychlé)
                mapa_comad = zpracuj_comad_feed()
                mapa_eltap = zpracuj_eltap_feed()
                
                # 3. Zpracuje objednávky
                zpracuj_objednavky(access_token, mapa_comad, mapa_eltap, ORDER_FEED_URL, stav)
                
                # 4. Úspěšný konec - aktualizace času v JSONu
                stav['last_sync'] = current_run_time
                stav['last_run_status'] = 'success'
                stav['updated_at'] = current_run_time
                uloz_stav(stav)
                print(f"\n🎉 Vše hotovo. Příště budeme pokračovat od: {current_run_time}")
                
            except Exception as e:
                print(f"❌ Došlo k nečekané chybě: {e}")
                print("⚠️ Čas synchronizace objednávek NEBYL posunut, zkusíme to příště znovu.")