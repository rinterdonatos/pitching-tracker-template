# Putting the Demo Online (PythonAnywhere, free)

This gets the site running at `https://<yourname>.pythonanywhere.com` so anyone
can open it in a browser. Takes about 30 minutes.

## 1. Push the code to GitHub

From this folder (the `.gitignore` already keeps the database and uploads out):

```
git init
git add .
git commit -m "Swarm Baseball tracker"
```

Create an empty repo on github.com, then:

```
git remote add origin https://github.com/YOURNAME/swarm-tracker.git
git push -u origin main
```

A **private** repo is fine - PythonAnywhere can still clone it if you're logged
into GitHub there, and it keeps the code out of public view.

## 2. Set up PythonAnywhere

1. Make a free account at pythonanywhere.com (the username becomes the URL).
2. Open a **Bash console** (Consoles tab) and run:

   ```
   git clone https://github.com/YOURNAME/swarm-tracker.git
   pip install --user flask
   ```

3. Go to the **Web** tab -> "Add a new web app" -> Manual configuration ->
   the newest Python version.
4. On the web app page set:
   - **Source code**: `/home/YOURNAME/swarm-tracker`
   - **WSGI configuration file**: click it and replace the contents with:

     ```python
     import sys
     sys.path.insert(0, "/home/YOURNAME/swarm-tracker")
     from app import app as application
     ```

5. Under **Static files** on the same page, add:
   - URL `/static/`  ->  Directory `/home/YOURNAME/swarm-tracker/static`
6. Hit the green **Reload** button.

## 3. First visit

Open `https://YOURNAME.pythonanywhere.com` - it will show the one-time setup
page. Create the owner account, add a few demo players/teams/stats, and it's
ready to show off. Invite links work for real from there.

## Notes

- The free tier keeps the SQLite database permanently, so demo data sticks.
- Video uploads: the free tier has 512 MB of storage total, so keep demo
  videos short or skip them.
- To update the site later: `git push` from your computer, then in a
  PythonAnywhere console `cd swarm-tracker && git pull`, then Reload on the
  Web tab.
- Password-reset emails/texts need the PHX_SMTP_* / PHX_TWILIO_* environment
  variables (see the comment near the top of app.py); without them, admins
  reset passwords from the Users page - fine for a demo.
