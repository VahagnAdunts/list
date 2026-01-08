from flask import Flask, render_template, jsonify
from bs4 import BeautifulSoup
import requests
import json
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import threading
import time

# Try to import cloudscraper for better anti-bot handling
try:
    import cloudscraper
    CLOUDSCRAPER_AVAILABLE = True
except ImportError:
    print("Warning: cloudscraper not available. Install it for better anti-bot handling: pip install cloudscraper")
    CLOUDSCRAPER_AVAILABLE = False
    cloudscraper = None

# Try to import psycopg2, but make it optional
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError as e:
    print(f"Warning: psycopg2 not available: {e}")
    print("Falling back to file-based storage")
    PSYCOPG2_AVAILABLE = False
    psycopg2 = None
    RealDictCursor = None

app = Flask(__name__)

# Configuration
LIST_AM_URL = os.environ.get('LIST_AM_URL', "https://www.list.am/category/60")
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 30))  # minutes
DATA_FILE = os.environ.get('DATA_FILE', "listings_data.json")

# Database configuration
DATABASE_URL = os.environ.get('DATABASE_URL')  # Render provides this automatically

# Store for new listings (in-memory cache)
new_listings = []
# Store for current listings (last 10 shown on page)
current_listings = []
last_check_time = None
is_checking = False
last_check_status = "Not checked yet"

def get_db_connection():
    """Get database connection"""
    if not PSYCOPG2_AVAILABLE or not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

def init_database():
    """Initialize database tables"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        
        # Create baseline table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS baseline_listings (
                id SERIAL PRIMARY KEY,
                listing_id VARCHAR(50) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create new_listings table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS new_listings (
                id SERIAL PRIMARY KEY,
                listing_id VARCHAR(50) NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                price VARCHAR(50),
                location TEXT,
                description TEXT,
                detected_at TIMESTAMP NOT NULL
            )
        """)
        
        # Create index for faster lookups
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_baseline_listing_id 
            ON baseline_listings(listing_id)
        """)
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_new_listings_detected_at 
            ON new_listings(detected_at DESC)
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully")
        return True
    except Exception as e:
        print(f"Error initializing database: {e}")
        if conn:
            conn.close()
        return False

def load_baseline():
    """Load the baseline listing IDs from database or file fallback"""
    # Try database first
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT listing_id FROM baseline_listings")
            rows = cur.fetchall()
            listing_ids = {row[0] for row in rows}
            cur.close()
            conn.close()
            print(f"Loaded {len(listing_ids)} baseline listings from database")
            return listing_ids
        except Exception as e:
            print(f"Error loading from database: {e}")
            if conn:
                conn.close()
    
    # Fallback to file (for local development without database)
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('listing_ids', []))
        except:
            return set()
    return set()

def save_baseline(listing_ids):
    """Save the baseline listing IDs to database or file fallback"""
    # Try database first
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            # Clear existing baseline
            cur.execute("DELETE FROM baseline_listings")
            # Insert new baseline
            for listing_id in listing_ids:
                cur.execute(
                    "INSERT INTO baseline_listings (listing_id) VALUES (%s) ON CONFLICT (listing_id) DO NOTHING",
                    (listing_id,)
                )
            conn.commit()
            cur.close()
            conn.close()
            print(f"Saved {len(listing_ids)} baseline listings to database")
            return
        except Exception as e:
            print(f"Error saving to database: {e}")
            if conn:
                conn.close()
    
    # Fallback to file (for local development without database)
    data = {
        'listing_ids': list(listing_ids),
        'last_updated': datetime.now().isoformat()
    }
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Baseline saved to file: {len(listing_ids)} listings")
    except Exception as e:
        print(f"Warning: Could not save baseline to file: {e}")

def save_new_listing(listing):
    """Save a new listing to database"""
    if not PSYCOPG2_AVAILABLE:
        return
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO new_listings (listing_id, url, title, price, location, description, detected_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                listing['id'],
                listing['url'],
                listing.get('title', ''),
                listing.get('price', ''),
                listing.get('location', ''),
                listing.get('description', ''),
                datetime.fromisoformat(listing.get('detected_at', datetime.now().isoformat()))
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Error saving new listing to database: {e}")
            if conn:
                conn.close()

def load_new_listings_from_db():
    """Load new listings from database"""
    if not PSYCOPG2_AVAILABLE:
        return []
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT listing_id as id, url, title, price, location, description, 
                       detected_at::text as detected_at
                FROM new_listings
                ORDER BY detected_at DESC
                LIMIT 100
            """)
            rows = cur.fetchall()
            listings = [dict(row) for row in rows]
            cur.close()
            conn.close()
            return listings
        except Exception as e:
            print(f"Error loading new listings from database: {e}")
            if conn:
                conn.close()
    return []

def extract_listings():
    """Extract listing information from the page"""
    try:
        # Try multiple approaches to avoid blocking
        approaches = [
            # Approach 1: Full browser headers
            {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9,hy;q=0.8,ru;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br, zstd',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            },
            # Approach 2: Simpler headers
            {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            # Approach 3: Minimal headers
            {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            }
        ]
        
        response = None
        
        # First, try cloudscraper if available (best for bypassing Cloudflare/anti-bot)
        if CLOUDSCRAPER_AVAILABLE:
            try:
                print("Trying cloudscraper to bypass anti-bot measures...")
                scraper = cloudscraper.create_scraper(
                    browser={
                        'browser': 'chrome',
                        'platform': 'darwin',
                        'desktop': True
                    }
                )
                response = scraper.get(LIST_AM_URL, timeout=30)
                if response.status_code == 200:
                    print("Successfully fetched page using cloudscraper")
                else:
                    print(f"Cloudscraper got status {response.status_code}, trying other approaches...")
                    response = None
            except Exception as e:
                print(f"Cloudscraper error: {e}, trying other approaches...")
                response = None
        
        # If cloudscraper didn't work, try regular requests with different headers
        if not response or response.status_code != 200:
            for i, headers in enumerate(approaches):
                try:
                    session = requests.Session()
                    session.headers.update(headers)
                    
                    # First, visit the main page to get cookies
                    try:
                        session.get('https://www.list.am/', timeout=10)
                        time.sleep(0.5)  # Small delay
                    except:
                        pass
                    
                    # Now get the actual page
                    response = session.get(LIST_AM_URL, timeout=30, allow_redirects=True)
                    
                    if response.status_code == 200:
                        print(f"Successfully fetched page using approach {i+1}")
                        break
                    elif response.status_code == 403:
                        print(f"Got 403 with approach {i+1}, trying next...")
                        continue
                    else:
                        print(f"Got status {response.status_code} with approach {i+1}, trying next...")
                        continue
                except Exception as e:
                    print(f"Error with approach {i+1}: {e}")
                    continue
        
        if not response or response.status_code != 200:
            error_msg = f"Failed to fetch page. Status: {response.status_code if response else 'No response'}"
            print(error_msg)
            return [], set()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        listings = []
        seen_ids = set()
        
        # Try multiple selectors to find listings
        # Method 1: Find all listing links - they have URLs like /item/12345678
        listing_links = soup.find_all('a', href=lambda x: x and '/item/' in str(x))
        
        # Method 2: If no links found, try finding divs with data attributes or specific classes
        if not listing_links:
            print("No links with /item/ found, trying alternative selectors...")
            # Try finding elements with item IDs in data attributes
            items_with_data = soup.find_all(attrs={'data-id': True})
            for item in items_with_data:
                item_id = item.get('data-id')
                if item_id and item_id.isdigit():
                    # Try to find link within this element
                    link = item.find('a', href=lambda x: x and '/item/' in str(x))
                    if link:
                        listing_links.append(link)
        
        # Method 3: Try finding by class names that might contain listings
        if not listing_links:
            print("Trying to find listings by class names...")
            # Common class patterns for listing containers
            possible_containers = soup.find_all(['div', 'article'], class_=lambda x: x and any(
                keyword in str(x).lower() for keyword in ['item', 'listing', 'ad', 'card', 'product']
            ))
            for container in possible_containers:
                link = container.find('a', href=lambda x: x and '/item/' in str(x))
                if link and link not in listing_links:
                    listing_links.append(link)
        
        print(f"Found {len(listing_links)} potential listing links")
        
        for link in listing_links:
            href = link.get('href', '')
            if not href:
                continue
                
            # Handle both relative and absolute URLs
            if href.startswith('/'):
                full_href = f"https://www.list.am{href}"
            elif href.startswith('http'):
                full_href = href
            else:
                continue
            
            if '/item/' in href:
                # Extract item ID from URL like /item/21414358?ld_src=2
                try:
                    item_id = href.split('/item/')[1].split('?')[0].split('&')[0].strip()
                except:
                    continue
                
                if item_id and item_id.isdigit() and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    
                    # Extract listing details
                    listing_data = {
                        'id': item_id,
                        'url': full_href.split('?')[0],
                        'title': '',
                        'price': '',
                        'location': '',
                        'description': ''
                    }
                    
                    # Get the full text content of the link
                    link_text = link.get_text(separator=' ', strip=True)
                    
                    # Extract price (usually at the start: $XXX,XXX or XXX,XXX ֏ or AMD)
                    import re
                    price_patterns = [
                        r'(\$[\d,]+)',
                        r'([\d,]+ ֏)',
                        r'([\d,]+ AMD)',
                        r'([\d,]+ դր)',
                    ]
                    price_match = None
                    for pattern in price_patterns:
                        price_match = re.search(pattern, link_text)
                        if price_match:
                            break
                    
                    if price_match:
                        listing_data['price'] = price_match.group(1)
                        # Remove price from text to get title
                        for pattern in price_patterns:
                            link_text = re.sub(pattern, '', link_text, count=1).strip()
                            if price_match.group(1) not in link_text:
                                break
                    
                    # The link text usually contains: price + type + description
                    listing_data['title'] = link_text if link_text else f"Listing {item_id}"
                    
                    # Look for location/description in parent container
                    parent = link.parent
                    max_depth = 3
                    depth = 0
                    while parent and depth < max_depth:
                        # Find all text in the parent container
                        parent_texts = []
                        for elem in parent.find_all(['div', 'span', 'p'], recursive=False):
                            text = elem.get_text(separator=' ', strip=True)
                            if text and len(text) > 5 and text not in link_text:
                                parent_texts.append(text)
                        
                        # Look for location info (contains district names or քմ, սեն)
                        districts = ['Կենտրոն', 'Արաբկիր', 'Աջափնյակ', 'Շենգավիթ', 'Նոր Նորք', 
                                    'Մալաթիա', 'Դավթաշեն', 'Ավան', 'Էրեբունի', 'Զովունի', 'Դավիթաշեն']
                        for text in parent_texts:
                            # Check if it's location info
                            if any(district in text for district in districts) or ('քմ' in text and 'սեն' in text):
                                listing_data['location'] = text
                                break
                            # Otherwise use as description if we don't have one
                            elif not listing_data['description'] and len(text) > 20:
                                listing_data['description'] = text
                        
                        if listing_data['location'] or listing_data['description']:
                            break
                            
                        parent = parent.parent
                        depth += 1
                    
                    # If we still don't have description, use title
                    if not listing_data['description']:
                        listing_data['description'] = listing_data['title']
                    
                    listings.append(listing_data)
        
        print(f"Extracted {len(listings)} unique listings")
        if len(listings) == 0 and len(listing_links) > 0:
            print(f"Warning: Found {len(listing_links)} links but extracted 0 listings. HTML structure may have changed.")
            # Debug: print first few links
            for i, link in enumerate(listing_links[:3]):
                print(f"  Link {i+1}: href={link.get('href', '')[:100]}")
        
        return listings, seen_ids
    except Exception as e:
        print(f"Error extracting listings: {str(e)}")
        import traceback
        traceback.print_exc()
        return [], set()

def check_for_new_listings():
    """Check for new listings and update the baseline"""
    global new_listings, current_listings, last_check_time, is_checking, last_check_status
    
    if is_checking:
        return
    
    is_checking = True
    try:
        print(f"Checking for new listings at {datetime.now()}")
        last_check_status = "Checking..."
        
        # Extract current listings
        extracted_listings, current_ids = extract_listings()
        
        if not current_ids:
            print("No listings found. This could mean:")
            print("  1. The website is blocking requests (403 error)")
            print("  2. The HTML structure has changed")
            print("  3. There are no listings on the page")
            print("  4. Network connectivity issues")
            last_check_status = "Error: No listings found. Website may be blocking requests or HTML structure changed."
            is_checking = False
            return
        
        # Store last 10 current listings (for display) - update global
        current_listings.clear()
        current_listings.extend(extracted_listings[:10])
        print(f"Found {len(current_ids)} total listings on page")
        
        # Load baseline
        baseline_ids = load_baseline()
        
        # If baseline is empty, initialize it with current listings
        if not baseline_ids:
            print("Initializing baseline with current listings...")
            save_baseline(current_ids)
            new_listings = []
            last_check_time = datetime.now()
            last_check_status = f"Baseline initialized with {len(current_ids)} listings"
            is_checking = False
            return
        
        # Find new listings
        new_ids = current_ids - baseline_ids
        
        if new_ids:
            print(f"Found {len(new_ids)} new listing(s)!")
            new_listings_data = [listing for listing in extracted_listings if listing['id'] in new_ids]
            
            # Add timestamp to each new listing
            for listing in new_listings_data:
                listing['detected_at'] = datetime.now().isoformat()
                # Save to database
                save_new_listing(listing)
            
            # Prepend new listings to the list (most recent first)
            new_listings = new_listings_data + new_listings
            
            # Keep only last 100 new listings to avoid memory issues
            new_listings = new_listings[:100]
            
            # Update baseline
            save_baseline(current_ids)
            last_check_status = f"Found {len(new_ids)} new listing(s)!"
        else:
            print("No new listings found.")
            last_check_status = f"Checked {len(current_ids)} listings - no new ones"
        
        last_check_time = datetime.now()
    except Exception as e:
        print(f"Error checking for new listings: {str(e)}")
        last_check_status = f"Error: {str(e)}"
        import traceback
        traceback.print_exc()
    finally:
        is_checking = False

# Initialize database on startup
def init_app():
    """Initialize database and load existing data"""
    if DATABASE_URL:
        print("Initializing database...")
        if init_database():
            print("Loading new listings from database...")
            global new_listings
            new_listings = load_new_listings_from_db()
            print(f"Loaded {len(new_listings)} existing new listings from database")
    else:
        print("No DATABASE_URL found - using file-based storage (data will be lost on restart)")

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=check_for_new_listings,
    trigger="interval",
    minutes=CHECK_INTERVAL,
    id='check_listings',
    name='Check for new listings every 30 minutes',
    replace_existing=True
)
scheduler.start()

# Run initial check after a short delay
def initial_check():
    time.sleep(5)  # Wait for app to start
    check_for_new_listings()

# Initialize app
init_app()
threading.Thread(target=initial_check, daemon=True).start()

@app.route('/')
def index():
    """Main page showing new listings"""
    return render_template('index.html', 
                         listings=new_listings,
                         current_listings=current_listings,
                         last_check=last_check_time,
                         last_check_status=last_check_status,
                         check_interval=CHECK_INTERVAL)

@app.route('/api/listings')
def api_listings():
    """API endpoint for listings"""
    return jsonify({
        'new_listings': new_listings,
        'current_listings': current_listings,
        'last_check': last_check_time.isoformat() if last_check_time else None,
        'last_check_status': last_check_status,
        'check_interval': CHECK_INTERVAL
    })

@app.route('/api/check-now')
def check_now():
    """Manually trigger a check"""
    if is_checking:
        return jsonify({'status': 'already_checking'}), 409
    
    threading.Thread(target=check_for_new_listings, daemon=True).start()
    return jsonify({'status': 'checking_started'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

