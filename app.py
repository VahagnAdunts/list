from flask import Flask, render_template, jsonify
from bs4 import BeautifulSoup
import requests
import json
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import threading
import time

app = Flask(__name__)

# Configuration
LIST_AM_URL = os.environ.get('LIST_AM_URL', "https://www.list.am/category/60")
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 30))  # minutes
DATA_FILE = os.environ.get('DATA_FILE', "listings_data.json")

# Store for new listings
new_listings = []
last_check_time = None
is_checking = False

def load_baseline():
    """Load the baseline listing IDs from environment variable or file"""
    # Try environment variable first (persists on Render)
    baseline_env = os.environ.get('BASELINE_LISTINGS')
    if baseline_env:
        try:
            data = json.loads(baseline_env)
            return set(data.get('listing_ids', []))
        except:
            pass
    
    # Fallback to file (for local development)
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('listing_ids', []))
        except:
            return set()
    return set()

def save_baseline(listing_ids):
    """Save the baseline listing IDs to file"""
    data = {
        'listing_ids': list(listing_ids),
        'last_updated': datetime.now().isoformat()
    }
    
    # Note: On Render's free tier, the filesystem is ephemeral
    # Files persist during the service's lifetime but are lost on restart
    # The baseline will be re-initialized on restart (first check creates new baseline)
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Baseline saved: {len(listing_ids)} listings")
    except Exception as e:
        print(f"Warning: Could not save baseline to file: {e}")

def extract_listings():
    """Extract listing information from the page"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        response = requests.get(LIST_AM_URL, headers=headers, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        listings = []
        seen_ids = set()
        
        # Find all listing links - they have URLs like /item/12345678
        listing_links = soup.find_all('a', href=lambda x: x and '/item/' in x)
        
        for link in listing_links:
            href = link.get('href', '')
            if '/item/' in href:
                # Extract item ID from URL like /item/21414358?ld_src=2
                item_id = href.split('/item/')[1].split('?')[0].split('&')[0]
                
                if item_id and item_id.isdigit() and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    
                    # Extract listing details
                    listing_data = {
                        'id': item_id,
                        'url': f"https://www.list.am{href.split('?')[0]}",
                        'title': '',
                        'price': '',
                        'location': '',
                        'description': ''
                    }
                    
                    # Get the full text content of the link
                    link_text = link.get_text(separator=' ', strip=True)
                    
                    # Extract price (usually at the start: $XXX,XXX or XXX,XXX ֏)
                    import re
                    price_match = re.search(r'(\$[\d,]+|[\d,]+ ֏)', link_text)
                    if price_match:
                        listing_data['price'] = price_match.group(1)
                        # Remove price from text to get title
                        link_text = link_text.replace(price_match.group(1), '', 1).strip()
                    
                    # The link text usually contains: price + type + description
                    # Split and extract
                    parts = link_text.split(' ', 2)
                    if len(parts) >= 2:
                        listing_data['title'] = ' '.join(parts[1:]) if len(parts) > 1 else link_text
                    else:
                        listing_data['title'] = link_text
                    
                    # Look for location/description in parent container
                    parent = link.parent
                    if parent:
                        # Find all text in the parent container
                        parent_texts = []
                        for elem in parent.find_all(['div', 'span', 'p', 'generic']):
                            text = elem.get_text(separator=' ', strip=True)
                            if text and len(text) > 5:
                                parent_texts.append(text)
                        
                        # Look for location info (contains district names or քմ, սեն)
                        districts = ['Կենտրոն', 'Արաբկիր', 'Աջափնյակ', 'Շենգավիթ', 'Նոր Նորք', 
                                    'Մալաթիա', 'Դավթաշեն', 'Ավան', 'Էրեբունի', 'Զովունի']
                        for text in parent_texts:
                            # Check if it's location info
                            if any(district in text for district in districts) or ('քմ' in text and 'սեն' in text):
                                listing_data['location'] = text
                                break
                            # Otherwise use as description if we don't have one
                            elif not listing_data['description'] and len(text) > 20:
                                listing_data['description'] = text
                    
                    # If we still don't have description, use title
                    if not listing_data['description']:
                        listing_data['description'] = listing_data['title']
                    
                    listings.append(listing_data)
        
        print(f"Extracted {len(listings)} listings")
        return listings, seen_ids
    except Exception as e:
        print(f"Error extracting listings: {str(e)}")
        import traceback
        traceback.print_exc()
        return [], set()

def check_for_new_listings():
    """Check for new listings and update the baseline"""
    global new_listings, last_check_time, is_checking
    
    if is_checking:
        return
    
    is_checking = True
    try:
        print(f"Checking for new listings at {datetime.now()}")
        
        # Extract current listings
        current_listings, current_ids = extract_listings()
        
        if not current_ids:
            print("No listings found. Skipping check.")
            is_checking = False
            return
        
        # Load baseline
        baseline_ids = load_baseline()
        
        # If baseline is empty, initialize it with current listings
        if not baseline_ids:
            print("Initializing baseline with current listings...")
            save_baseline(current_ids)
            new_listings = []
            last_check_time = datetime.now()
            is_checking = False
            return
        
        # Find new listings
        new_ids = current_ids - baseline_ids
        
        if new_ids:
            print(f"Found {len(new_ids)} new listing(s)!")
            new_listings_data = [listing for listing in current_listings if listing['id'] in new_ids]
            
            # Add timestamp to each new listing
            for listing in new_listings_data:
                listing['detected_at'] = datetime.now().isoformat()
            
            # Prepend new listings to the list (most recent first)
            new_listings = new_listings_data + new_listings
            
            # Keep only last 100 new listings to avoid memory issues
            new_listings = new_listings[:100]
            
            # Update baseline
            save_baseline(current_ids)
        else:
            print("No new listings found.")
        
        last_check_time = datetime.now()
    except Exception as e:
        print(f"Error checking for new listings: {str(e)}")
    finally:
        is_checking = False

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

threading.Thread(target=initial_check, daemon=True).start()

@app.route('/')
def index():
    """Main page showing new listings"""
    return render_template('index.html', 
                         listings=new_listings, 
                         last_check=last_check_time,
                         check_interval=CHECK_INTERVAL)

@app.route('/api/listings')
def api_listings():
    """API endpoint for listings"""
    return jsonify({
        'listings': new_listings,
        'last_check': last_check_time.isoformat() if last_check_time else None,
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

