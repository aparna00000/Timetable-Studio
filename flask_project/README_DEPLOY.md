# 🚀 Deployment Guide: Timetable Studio

Your application is now ready for the world! Here is how to publish it online for free using **PythonAnywhere** (highly recommended for Flask).

## Step 1: Create an Account
Go to [PythonAnywhere](https://www.pythonanywhere.com/) and create a "Beginner" (Free) account.

## Step 2: Upload your Files
1. Log in to your dashboard.
2. Go to the **"Files"** tab.
3. Create a new directory called `timetable_studio`.
4. Upload all the files from your local `flask_project` folder into this directory.
   - *Tip: Zip your folder locally, upload it, and use the PythonAnywhere console to `unzip` it.*

## Step 3: Set up the Web App
1. Go to the **"Web"** tab.
2. Click **"Add a new web app"**.
3. Choose **Manual Configuration**.
4. Select **Python 3.10** (or higher).

## Step 4: Configure Virtual Environment
Open a **Bash Console** from your PythonAnywhere dashboard and run:
```bash
mkvirtualenv --python=/usr/bin/python3.10 myenv
pip install -r requirements.txt
```

## Step 5: Update the WSGI Configuration
In the **"Web"** tab, find the link to the **WSGI configuration file**. Replace its entire content with:
```python
import sys
import os

# Add your project directory to the sys.path
path = '/home/YOUR_USERNAME/timetable_studio'
if path not in sys.path:
    sys.path.append(path)

os.chdir(path)

from app import app as application
```
*(Replace `YOUR_USERNAME` with your actual PythonAnywhere username)*

## Step 6: Reload and Launch
1. Go back to the **"Web"** tab.
2. Set the "Virtualenv" path to `/home/YOUR_USERNAME/.virtualenvs/myenv`.
3. Click **"Reload"** at the top.
4. Visit `yourusername.pythonanywhere.com`!

---

### Important Production Tips:
- **Database:** The `timetable.db` will be created automatically on the server when the first user signs up.
- **Secret Key:** For extra security, change the `app.secret_key` in `app.py` to something long and random before you publish.
- **Debug Mode:** The server will automatically run in production mode (Debug: Off).
