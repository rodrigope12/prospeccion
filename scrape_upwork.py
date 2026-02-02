import asyncio
import random
import logging
import json
import re  # Asegurar que re está importado
from playwright.async_api import async_playwright

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

async def inject_start_button(page):
    """
    Injects a floating button into the Upwork page.
    The script will wait until this button is clicked to start extraction.
    """
    await page.evaluate("""() => {
        // Prevent multiple buttons if the script is re-run or feed refreshes
        if (document.getElementById('agent-start-scrape')) return;

        const btn = document.createElement('button');
        btn.id = 'agent-start-scrape';
        btn.innerText = '⚡️ EXTRAER TRABAJOS VISIBLES';
        btn.style.position = 'fixed';
        btn.style.top = '20px';
        btn.style.right = '20px';
        btn.style.zIndex = '99999';
        btn.style.padding = '15px 25px';
        btn.style.backgroundColor = '#14a800';
        btn.style.color = 'white';
        btn.style.border = 'none';
        btn.style.borderRadius = '5px';
        btn.style.fontSize = '18px';
        btn.style.fontWeight = 'bold';
        btn.style.cursor = 'pointer';
        btn.style.boxShadow = '0 4px 15px rgba(0,0,0,0.3)';
        
        btn.onclick = () => {
            btn.innerText = '⏳ Extrayendo datos...';
            btn.style.backgroundColor = '#666';
            btn.disabled = true;
            window.agent_should_start = true;
        };
        
        document.body.appendChild(btn);
        window.agent_should_start = false;
    }""")

async def extract_jobs(page):
    """
    Extracts all job data currently visible on the page.
    """
    jobs_data = []
    
    # Intento de múltiples selectores para encontrar las tarjetas
    potential_selectors = [
        'article',                     # Estándar semántico
        '.up-card-section',            # Clásica clase de Upwork
        'section.air3-card-section',   # Nueva interfaz Air3
        'div[data-test="job-tile-list"] > section', 
        '.job-tile',
        '.up-card-list-section'
    ]

    job_articles = []
    used_selector = ""

    for selector in potential_selectors:
        found = await page.locator(selector).all()
        if len(found) > 0:
            job_articles = found
            used_selector = selector
            logging.info(f"Selector '{selector}' funcionó: {len(found)} tarjetas encontradas.")
            break
    
    if len(job_articles) == 0:
        logging.error("❌ NO SE ENCONTRARON TRABAJOS CON NINGÚN SELECTOR.")
        logging.info("Guardando 'debug_page.html' para analizar la estructura...")
        html = await page.content()
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Revisa 'debug_page.html' o envíaselo al desarrollador.")
        return []

    logging.info(f"Detectados {len(job_articles)} trabajos en pantalla. Iniciando extracción...")

    # Helper para probar múltiples selectores
    async def get_text_from_selectors(element, selectors):
        for sel in selectors:
            try:
                el = element.locator(sel).first
                if await el.count() > 0:
                    text = await el.text_content()
                    if text and text.strip():
                        return text.strip()
            except:
                continue
        return None

    for article in job_articles:
        try:
            # --- 1. TITLE ---
            title = await get_text_from_selectors(article, [
                'h3.job-tile-title a', 
                'h4.job-tile-title a',
                '.job-title a',
                'a[data-test="job-title-link"]'
            ]) or "N/A"

            # --- 2. URL ---
            url = "N/A"
            try:
                # El link suele estar en el mismo selector del título
                title_link = article.locator('a[href*="/jobs/"], h3 a, h4 a').first
                if await title_link.count() > 0:
                    href = await title_link.get_attribute('href')
                    if href:
                        url = f"https://www.upwork.com{href}"
            except:
                pass

            # --- 3. DESCRIPTION ---
            description = await get_text_from_selectors(article, [
                '[data-test="job-description-text"]',
                '.job-description',
                '.air3-line-clamp',
                'p.mb-0'
            ]) or "N/A"

            # --- 4. BUDGET / COST ---
            # En Air3, a veces es una lista ul/li o spans sueltos
            # Intentamos selectores específicos primero
            budget_info = await get_text_from_selectors(article, [
                '[data-test="job-type-label"]', # Legacy
                'ul.job-type',
                '[data-test="job-type"]',
                '.job-type-info'
            ])
            
            # Si falla, usamos Regex sobre todo el texto del artículo
            full_text = await article.text_content()
            if not budget_info:
                import re
                # Patrones comunes: "Hourly: $10-$30", "Fixed-price: $500", "Est. Budget: $100"
                # Buscamos líneas que contengan $
                cost_matches = re.findall(r"(?:Hourly|Fixed-price|Est\. Budget).*?\$[\d,]+(?:-\$[\d,]+)?", full_text, re.IGNORECASE)
                if cost_matches:
                    budget_info = " | ".join(cost_matches)
                else:
                    budget_info = "N/A"

            # --- 5. PAYMENT VERIFIED ---
            payment_verified = False
            if "Payment verified" in full_text or "Pago verificado" in full_text:
                payment_verified = True
            elif await article.locator('.payment-verified, .verified-badge').count() > 0:
                payment_verified = True

            # --- 6. CLIENT RATING (Deep Scan) ---
            rating = "N/A"
            # Strategy A: Text selectors
            rating = await get_text_from_selectors(article, [
                '.up-rating-score',
                '[data-test="client-rating"]',
                '.air3-rating-value span',
                '.air3-rating-value-text',
                'span.air3-rating'
            ])
            
            # Strategy B: ARIA labels or SVG Titles (Common in stars)
            if not rating:
                try:
                    # Buscar elementos con 'aria-label' que contengan "stars" o "out of"
                    star_el = article.locator('[aria-label*="star"], [aria-label*="rating"]').first
                    if await star_el.count() > 0:
                        aria = await star_el.get_attribute("aria-label")
                        if aria:
                             # Extract numbers like "5.0" or "4.9"
                             m = re.search(r"(\d\.\d)", aria)
                             if m: rating = m.group(1)
                except:
                    pass

            # Strategy C: Regex on Full Text (Last Resort)
            if not rating:
                # Buscamos "5.0" seguido de "stars" o al inicio de una línea de review
                full_text_clean = full_text.replace("\n", " ")
                # Pattern: " 5.0 of 5 stars" or " 4.9 " roughly
                rating_match = re.search(r"\b([0-4]\.\d|5\.0)\b", full_text_clean)
                if rating_match:
                     # Validar que parezca un rating (cerca de 'stars' o 'reviews')
                     # Pero para ser agresivos, si encontramos un 5.0 flotando, lo tomamos si no hay nada más
                     rating = rating_match.group(1)

            if not rating:
                rating = "N/A"

            # --- CONSTANCY CHECK ---
            # Budget Cleanup
            budget_info = budget_info.replace("\n", " ").strip()
            if len(budget_info) > 100: # Si extrajo demasiado texto por error
                budget_info = budget_info[:100] + "..."

            # Solo agregar si tiene título válido (filtramos esqueletos o basura)
            if title and title != "N/A":
                jobs_data.append({
                    "Nombre del Proyecto": title,
                    "Descripción": description,
                    "Costo / Presupuesto": budget_info,
                    "Pago Verificado": payment_verified,
                    "Calificación del Empleador": rating
                })
            
        except Exception as e:
            logging.error(f"Error parseando una tarjeta de trabajo: {e}")
            continue
            
    return jobs_data

async def main():
    print("Iniciando navegador...")
    async with async_playwright() as p:
        # Argumentos para evitar detección de bot
        args = [
            "--disable-blink-features=AutomationControlled", 
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized"
        ]

        try:
            print("Intentando lanzar Google Chrome (Modo Stealth)...")
            browser = await p.chromium.launch(headless=False, channel="chrome", args=args)
        except Exception as e:
            print(f"Chrome no encontrado ({e}). Usando Chromium (Modo Stealth)...")
            browser = await p.chromium.launch(headless=False, args=args)

        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        )
        
        # Inyectar script para ocultar propiedades de automatización
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = await context.new_page()

        print("Navegando a Upwork...")
        try:
            await page.goto("https://www.upwork.com/nx/find-work/most-recent", timeout=60000)
        except:
            print("La página tardó en cargar, pero continuamos...")

        print("\n" + "="*60)
        print(" INSTRUCCIONES:")
        print(" 1. LOGUEATE en Upwork si es necesario.")
        print(" 2. Una vez que veas tus trabajos, busca el botón verde flotante.")
        print(" 3. Carga más trabajos manualmente si quieres.")
        print(" 4. Click en: '⚡️ EXTRAER TRABAJOS VISIBLES'")
        print("="*60 + "\n")

        # Inyectar el botón constantemente en segundo plano para asegurar que aparezca
        # No esperamos a un selector específico porque puede variar
        async def keep_injecting_button():
            while True:
                try:
                    await inject_start_button(page)
                except:
                    pass
                await asyncio.sleep(2)
                if getattr(page, "is_closed", lambda: False)():
                    break
        
        # Empezamos la inyección en "background" (sin bloquear)
        asyncio.create_task(keep_injecting_button())

        # Esperamos a que el usuario presione el botón (variable window.agent_should_start)
        while True:
            try:
                should_start = await page.evaluate("window.agent_should_start")
                if should_start:
                    break
            except:
                # Si la página se está recargando o el contexto cambió
                pass
            await asyncio.sleep(1)

        logging.info("Extrayendo datos de la página...")
        
        # Extraction - Usamos un selector más genérico por si cambió el data-test
        jobs = await extract_jobs(page)

        # Save to JSON
        output_file = "upwork_jobs.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)

        print(f"\n✅ ÉXITO: Se guardaron {len(jobs)} trabajos en '{output_file}'.")
        print("Proceso terminado. Puedes cerrar el navegador.")
        
        await asyncio.sleep(5)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
