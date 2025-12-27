import random
import time
import logging
from playwright.sync_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

class StealthManager:
    def __init__(self, config: dict):
        self.config = config
        self.user_agents = config.get('anti_detection', {}).get('user_agents', [])
        self.min_delay = config.get('anti_detection', {}).get('min_delay', 5)
        self.max_delay = config.get('anti_detection', {}).get('max_delay', 15)

    def get_random_user_agent(self) -> str:
        if not self.user_agents:
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        return random.choice(self.user_agents)

    def create_context(self, browser: Browser) -> BrowserContext:
        """Creates a browser context with stealth settings."""
        ua = self.get_random_user_agent()
        logger.info(f"Creating context with User-Agent: {ua}")
        
        # Randomize viewport to look more human
        width = random.randint(1200, 1920)
        height = random.randint(800, 1080)
        
        context = browser.new_context(
            user_agent=ua,
            viewport={'width': width, 'height': height},
            locale="fr-FR",
            timezone_id="Europe/Paris",
            geolocation={"latitude": 48.8566 + random.uniform(-0.1, 0.1), 
                         "longitude": 2.3522 + random.uniform(-0.1, 0.1)},
            permissions=["geolocation"],
            color_scheme="light",
            device_scale_factor=random.choice([1, 1.25, 1.5, 2]),
            is_mobile=False,
            has_touch=False,
            accept_downloads=False,
            java_script_enabled=True,
            bypass_csp=False,  # Don't bypass CSP - it can be detected
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
        )
        
        return context

    def apply_stealth_to_page(self, page: Page):
        """Applies advanced stealth scripts to evade bot detection."""
        
        # Comprehensive stealth script
        stealth_js = """
        () => {
            // Override navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });
            
            // Override navigator.plugins to have realistic plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
                    ];
                    plugins.item = (index) => plugins[index];
                    plugins.namedItem = (name) => plugins.find(p => p.name === name);
                    plugins.refresh = () => {};
                    return plugins;
                },
                configurable: true
            });
            
            // Override navigator.languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['fr-FR', 'fr', 'en-US', 'en'],
                configurable: true
            });
            
            // Override navigator.platform
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
                configurable: true
            });
            
            // Override navigator.hardwareConcurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
                configurable: true
            });
            
            // Override navigator.deviceMemory
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
                configurable: true
            });
            
            // Fix permissions query
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            
            // Remove automation-related window properties
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
            
            // Override chrome runtime
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
            
            // Fix iframe contentWindow
            const originalContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                get: function() {
                    const iframe = originalContentWindow.get.call(this);
                    try {
                        if (iframe && iframe.navigator) {
                            Object.defineProperty(iframe.navigator, 'webdriver', {
                                get: () => undefined,
                                configurable: true
                            });
                        }
                    } catch(e) {}
                    return iframe;
                }
            });
            
            // Override WebGL vendor and renderer
            const getParameterProxyHandler = {
                apply: function(target, thisArg, argumentsList) {
                    const param = argumentsList[0];
                    const gl = thisArg;
                    // UNMASKED_VENDOR_WEBGL
                    if (param === 37445) {
                        return 'Intel Inc.';
                    }
                    // UNMASKED_RENDERER_WEBGL
                    if (param === 37446) {
                        return 'Intel Iris OpenGL Engine';
                    }
                    return Reflect.apply(target, thisArg, argumentsList);
                }
            };
            
            // Apply WebGL overrides
            try {
                const canvas = document.createElement('canvas');
                const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                if (gl) {
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = new Proxy(getParameter, getParameterProxyHandler);
                }
            } catch(e) {}
            
            // Override toString to hide proxy/modifications
            const originalToString = Function.prototype.toString;
            Function.prototype.toString = function() {
                if (this === window.navigator.permissions.query) {
                    return 'function query() { [native code] }';
                }
                return originalToString.call(this);
            };
            
            // Console clear detection protection
            const originalClear = console.clear;
            console.clear = function() {
                return originalClear.apply(this, arguments);
            };
        }
        """
        
        # Add init script to run on every navigation
        page.add_init_script(stealth_js)
        
        logger.debug("Stealth scripts applied to page.")
        
    def random_sleep(self, min_override=None, max_override=None):
        """Sleeps for a random amount of time."""
        min_delay = min_override or self.min_delay
        max_delay = max_override or self.max_delay
        duration = random.uniform(min_delay, max_delay)
        logger.debug(f"Sleeping for {duration:.2f} seconds...")
        time.sleep(duration)

    def simulate_human_behavior(self, page: Page):
        """Simulates realistic human interactions."""
        try:
            # Random initial delay
            time.sleep(random.uniform(0.5, 1.5))
            
            # Move mouse to random positions with delays
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, 800)
                y = random.randint(100, 600)
                page.mouse.move(x, y, steps=random.randint(5, 15))
                time.sleep(random.uniform(0.1, 0.3))
            
            # Scroll gradually (like a human reading)
            scroll_distance = random.randint(200, 400)
            page.evaluate(f"window.scrollBy(0, {scroll_distance})")
            time.sleep(random.uniform(0.5, 1.0))
            
            # More scrolling
            scroll_distance = random.randint(300, 600)
            page.evaluate(f"window.scrollBy(0, {scroll_distance})")
            time.sleep(random.uniform(1.0, 2.0))
            
        except Exception as e:
            logger.warning(f"Failed to simulate human behavior: {e}")

    def wait_for_cloudflare(self, page: Page, timeout: int = 30000):
        """Wait for Cloudflare challenge to complete if present."""
        try:
            # Check for common Cloudflare indicators
            cf_indicators = [
                'div#cf-please-wait',
                'div.cf-browser-verification',
                'iframe[src*="challenges.cloudflare.com"]',
                'div#challenge-running',
            ]
            
            for indicator in cf_indicators:
                if page.locator(indicator).count() > 0:
                    logger.info("Cloudflare challenge detected, waiting...")
                    # Wait for the challenge to disappear
                    page.wait_for_selector(indicator, state='hidden', timeout=timeout)
                    time.sleep(random.uniform(2, 4))
                    break
                    
        except Exception as e:
            logger.debug(f"No Cloudflare challenge or timeout: {e}")

    def handle_cookie_consent(self, page: Page):
        """Try to handle cookie consent popups."""
        try:
            # Common cookie consent button selectors
            consent_selectors = [
                'button[id*="accept"]',
                'button[class*="accept"]',
                'button:has-text("Accepter")',
                'button:has-text("Accept")',
                'button:has-text("Tout accepter")',
                'button:has-text("J\'accepte")',
                'button[data-testid*="accept"]',
                '#didomi-notice-agree-button',
                '.didomi-continue-without-agreeing',
            ]
            
            for selector in consent_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=2000):
                        logger.info(f"Found cookie consent button: {selector}")
                        btn.click()
                        time.sleep(random.uniform(0.5, 1.5))
                        return True
                except:
                    continue
                    
        except Exception as e:
            logger.debug(f"Cookie consent handling: {e}")
        
        return False
