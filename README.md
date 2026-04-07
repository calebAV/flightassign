# FlightAssign — ATL Rolling Flight Assignment Automation

Automatically assigns ATL ramp operators to flights every 15 minutes during shift hours (Mon–Fri, 5:30 AM – 10:00 PM EDT) and posts the schedule to Slack.

## How It Works

1. Reads the weekly operator roster from a pinned `WEEKLY_SCHEDULE_JSON` message in Slack
2. Fetches live flight data from the AeroVect Fleet API
3. Assigns operators to flights using round-robin with break/spacing constraints
4. Posts the full schedule to `#flight-assign` as a new message every 15 minutes

## Setup (One-Time)

### 1. Create a Slack App

You need a Slack bot token so the script can read and post messages.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** → **From scratch**
2. Name it `FlightAssign Bot` and select the AeroVect workspace
3. Go to **OAuth & Permissions** and add these **Bot Token Scopes**:
   - `channels:history` (read messages from public channels)
   - `channels:read` (list channels)
   - `chat:write` (post messages)
4. Click **Install to Workspace** and approve
5. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
6. **Invite the bot to your channel**: In Slack, go to `#flight-assign`, type `/invite @FlightAssign Bot`

### 2. Create a GitHub Repository

1. Create a new repo on GitHub (e.g., `aerovect/flightassign`)
2. Push all the files from this folder to the repo:
   ```bash
   cd flightassign
   git init
   git add .
   git commit -m "Initial FlightAssign automation"
   git remote add origin https://github.com/aerovect/flightassign.git
   git push -u origin main
   ```

### 3. Add Secrets to GitHub

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret Name        | Value                                                        |
|--------------------|--------------------------------------------------------------|
| `SLACK_BOT_TOKEN`  | Your bot token (`xoxb-...`)                                  |
| `SLACK_CHANNEL_ID` | `C0AQEA7NR28` (the ID of #flight-assign)                     |
| `FLEET_API_URL`    | `https://beta.api.fleet.aerovect.com/flights?airport=ATL`    |

### 4. Enable the Workflow

The GitHub Action runs automatically once the repo has the workflow file. You can verify it's working:

1. Go to your repo → **Actions** tab
2. Click **ATL Flight Assignments** in the left sidebar
3. Click **Run workflow** → **Run workflow** to trigger a manual test run
4. Check `#flight-assign` in Slack for the new post

## How to Edit

### Change operator roster
Post a new `WEEKLY_SCHEDULE_JSON` message to `#flight-assign` in Slack — same format as before. The script always reads the most recent one.

### Change assignment rules
Edit `engine.py` and push to `main`. Changes take effect on the next scheduled run (within 15 minutes).

Key sections to know:
- **Break times**: `SHIFT1_BREAKS` and `SHIFT2_BREAKS` at the top of the file
- **Gate filter**: `GATE_PATTERN` regex — controls which gates are in scope
- **Spacing constraint**: Search for `timedelta(minutes=20)` in `run_assignments()`
- **Haulout offset**: Search for `timedelta(minutes=50)` — this is contractual, don't change it

### Change schedule timing
Edit `.github/workflows/flight-assignments.yml` and update the `cron` expressions.

### Run manually
Go to the repo's **Actions** tab → **ATL Flight Assignments** → **Run workflow**. Useful for testing or if something goes wrong.

## Troubleshooting

**No messages appearing in Slack:**
- Check the Actions tab for red (failed) runs and read the logs
- Verify the bot was invited to the channel (`/invite @FlightAssign Bot`)
- Verify the `SLACK_BOT_TOKEN` secret is correct

**Wrong operators showing up:**
- Check that the latest `WEEKLY_SCHEDULE_JSON` in Slack is correct
- The script uses the most recent one it finds in channel history

**Fleet API errors:**
- The script will post an error message to Slack automatically
- Check the Actions log for details
- It will retry on the next 15-minute cycle

## Important Notes

- The `SLACK_CHANNEL_ID` is a permanent Slack ID — renaming the channel won't break anything
- GitHub Actions cron can have up to ~5 minutes of delay — this is normal
- The free tier of GitHub Actions gives you 2,000 minutes/month. This automation uses ~4 min/day = ~80 min/month, well within limits
- If you need to stop the automation temporarily, go to Actions → click the workflow → three dots menu → **Disable workflow**
