# List.am New Listings Monitor

A web application that monitors List.am for new apartment listings and displays them on a web interface. The system checks for new listings every 30 minutes automatically.

## Features

- 🔍 Automatically monitors List.am category 60 (apartment sales)
- ⏰ Checks for new listings every 30 minutes
- 🌐 Web interface to view new listings
- 🔔 Shows listing details: price, title, description, location
- 🔗 Direct links to listings on List.am
- 📱 Responsive design

## Deployment to Render

### Prerequisites

1. A Render account (sign up at https://render.com)
2. Your code pushed to a Git repository (GitHub, GitLab, or Bitbucket)

### Steps

1. **Push your code to GitHub/GitLab/Bitbucket**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

2. **Create a new Web Service on Render**
   - Go to https://dashboard.render.com
   - Click "New +" → "Web Service"
   - Connect your repository
   - Select the repository and branch

3. **Configure the service**
   - **Name**: list-am-monitor (or any name you prefer)
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app.py`
   - **Plan**: Free tier is fine for this

4. **Create a PostgreSQL Database** (for persistent storage)
   - In Render dashboard, click "New +" → "PostgreSQL"
   - **Name**: list-am-db (or any name)
   - **Database**: list_am (or any name)
   - **User**: Will be auto-generated
   - **Region**: Same as your web service
   - **Plan**: Free tier is fine
   - Click "Create Database"
   - **Important**: Copy the "Internal Database URL" - you'll need it

5. **Link Database to Web Service**
   - Go to your Web Service settings
   - Scroll to "Connections" section
   - Click "Link" next to your PostgreSQL database
   - This automatically sets the `DATABASE_URL` environment variable

6. **Environment Variables** (important!)
   - **LIST_AM_URL**: Set this to your filtered List.am URL (e.g., `https://www.list.am/category/60?param1=value1&param2=value2`)
     - Go to List.am, apply your filters, copy the full URL from the address bar
     - In Render dashboard, go to Environment → Add Environment Variable
     - Key: `LIST_AM_URL`
     - Value: Your filtered URL
   - **CHECK_INTERVAL**: How often to check (in minutes, default: 30)
   - **DATABASE_URL**: Automatically set when you link the database (don't set manually)
   - **PORT**: Automatically set by Render (don't change this)

5. **Deploy**
   - Click "Create Web Service"
   - Render will build and deploy your application
   - Once deployed, you'll get a URL like `https://list-am-monitor.onrender.com`

## Local Testing

Before deploying to Render, you can test locally:

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variable** (optional, if you want to use a filtered URL):
   ```bash
   export LIST_AM_URL="https://www.list.am/category/60?your=filter&params=here"
   ```

3. **Run the application**:
   ```bash
   python app.py
   ```

4. **Open your browser**:
   - Navigate to `http://localhost:5000`
   - The app will perform an initial check after 5 seconds
   - Wait for the first check to complete (it will initialize the baseline)
   - New listings will appear after the second check (30 minutes later, or click "Check Now")

## How It Works

1. **Initial Check**: When the app starts, it performs an initial check after 5 seconds
2. **Baseline Creation**: On first run, it creates a baseline of all current listings
3. **Periodic Checks**: Every 30 minutes, it:
   - Fetches the current listings from List.am
   - Compares with the baseline
   - Identifies new listings
   - Updates the baseline
4. **Display**: New listings are shown on the web interface with all details

## Manual Check

You can manually trigger a check by clicking the "Check Now" button on the web interface.

## API Endpoints

- `GET /` - Main web interface
- `GET /api/listings` - JSON API for listings data
- `GET /api/check-now` - Manually trigger a check

## Configuration

You can modify the following in `app.py`:

- `LIST_AM_URL`: The URL to monitor (default: category 60)
- `CHECK_INTERVAL`: Check interval in minutes (default: 30)
- `DATA_FILE`: File to store baseline data (default: listings_data.json)

## Notes

- **Data Persistence**: The app uses PostgreSQL for persistent storage. All baseline listings and new listings are stored in the database and will persist across restarts.
- **Fallback**: If no database is configured, the app falls back to file-based storage (data will be lost on restart)
- New listings are kept in the database (last 100 shown on the web interface)
- The page auto-refreshes every 5 minutes
- Make sure your filters are applied in the URL before deploying
- The database tables are automatically created on first run

## Troubleshooting

- If no listings appear, check the Render logs for errors
- The first check initializes the baseline, so no new listings will show until the second check
- Make sure the List.am URL includes your filter parameters

