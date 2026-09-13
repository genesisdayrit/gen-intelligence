# Spotify Scheduled Jobs (APScheduler)

Ports the personal-ec2 `~/repos/spotify-api` crontab onto hub APScheduler. Access tokens refresh **on demand** (and again if a Spotify call returns 401). There is **no** `*/55 refresh_redis_token` job.

Sunday wrap-up email already used this pattern (`SPOTIFY_*` env → `refresh_spotify_access_token` at send time). The write jobs extend the same client.

## Jobs

| Job id | Local (`SYSTEM_TZ`) | Old crontab | What it does |
|---|---|---|---|
| `spotify_sync_shazam_to_library` | Every 15 minutes | `*/15 * * * * add_shazam_songs_to_libary_today.py` | Poll `SHAZAM_PLAYLIST_ID`, save new tracks to Liked Songs (~1.2s spacing so `added_at` is second-unique), advance hub Redis watermark only over tracks that were processed. |
| `spotify_sync_saved_today_to_half_year` | Every 15 minutes at :05/:20/:35/:50 | `5-59/15 * * * * add_songs_saved_today.py` | Find `{year} - 1/2` or `{year} - 2/2`, add Liked Songs saved today that are not already in it (same spacing). Offset is intentional: Shazam fills Liked Songs first. |
| `spotify_create_half_year_playlist` | Jan 1 and Jul 1 at 00:05 | `0 0 1 1,7 * create_this_half_year_playlist.py` | Create the playlist for the new half if it is missing. |

Not ported: `*/55 * * * * refresh_redis_token.py`. Durable secret is `SPOTIFY_REFRESH_TOKEN` in env. Hub Redis may cache `spotify_access_token` with a short TTL.

## Prerequisites

- Hub Redis (Docker `redis` service). This is **not** the personal-ec2 host Redis on `127.0.0.1:6379`.
- Spotify app credentials and a user refresh token with write scopes
- `SHAZAM_PLAYLIST_ID` (the Shazam-synced "My Shazam Tracks" playlist)

## Environment variables

```bash
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REFRESH_TOKEN=your_spotify_refresh_token
SHAZAM_PLAYLIST_ID=your_shazam_playlist_id
SYSTEM_TIMEZONE=America/Los_Angeles
# REDIS_HOST=redis   # set by docker-compose
# REDIS_PORT=6379
```

### Required scopes

The Sunday wrap-up path only needed `user-library-read`. Write jobs also need:

- `user-library-modify`
- `playlist-read-private`
- `playlist-modify-public`
- `playlist-modify-private`

If `SPOTIFY_REFRESH_TOKEN` was minted with only `user-library-read`, re-authorize the same Spotify app with the scopes above and replace the env value. Do not invent tokens.

Authorization (replace client id and a registered redirect URI):

```
https://accounts.spotify.com/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=YOUR_REDIRECT_URI&scope=user-library-read%20user-library-modify%20playlist-read-private%20playlist-modify-public%20playlist-modify-private
```

Exchange the `code` for a refresh token via `POST https://accounts.spotify.com/api/token` (`grant_type=authorization_code`). Put only that refresh token in `SPOTIFY_REFRESH_TOKEN`.

## Redis keys (hub only)

| Key | Purpose |
|---|---|
| `spotify_shazam_last_processed_added_at` | Watermark: Shazam playlist `added_at` of the last processed track (ISO8601 UTC). |
| `spotify_access_token` | Optional short-lived access-token cache (`expires_in` minus 60s). Never store the refresh token here. |

Host Redis used the same key names. Copy the watermark **once** on cutover. Do not point the hub at host Redis for tokens.

### Missing watermark

If the watermark key is missing, the Shazam job **seeds it to now and processes nothing**. That avoids dumping the historical Shazam playlist into Liked Songs on a fresh hub Redis.

This is **not** a permanent skip. Recovery:

1. Set `spotify_shazam_last_processed_added_at` in hub Redis to an earlier timestamp (or the host value), then
2. `POST /scheduler/jobs/spotify_sync_shazam_to_library/run`

On cutover, migrate the host watermark **before** the first successful hub run. If you forget, tracks added to Shazam between the last host cron and the seed-to-now instant are skipped until you rewind the watermark.

## Manual trigger

```bash
curl http://localhost:8000/scheduler/jobs

curl -X POST http://localhost:8000/scheduler/jobs/spotify_sync_shazam_to_library/run
curl -X POST http://localhost:8000/scheduler/jobs/spotify_sync_saved_today_to_half_year/run
curl -X POST http://localhost:8000/scheduler/jobs/spotify_create_half_year_playlist/run
```

## Behavior notes

- **Spacing:** ~1.2 seconds between Liked Songs saves and between playlist adds so Spotify `added_at` stays unique at second precision.
- **Already liked:** a Shazam track already in Liked Songs is not PUT again (re-saving resets `added_at`). The watermark still advances.
- **Failed save:** the watermark does not move past the failed track; the job raises and retries that track next run.
- **Half-year names:** January–June → `{year} - 1/2`; July–December → `{year} - 2/2`.
- **Logging:** never print tokens, refresh tokens, or `Authorization` headers. Historical `cron.log` on the host may contain tokens — do not copy those lines into hub logs.

## Cutover checklist

Do this after merge + deploy, **before** leaving both schedulers running.

1. Confirm `app/.env` has `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, and `SHAZAM_PLAYLIST_ID`. Recreate the app container so it picks up env (`docker compose up -d --force-recreate app`).
2. Confirm the refresh token has the write scopes. If a job returns HTTP 403, re-authorize (see [Required scopes](#required-scopes)) and replace `SPOTIFY_REFRESH_TOKEN`.
3. Migrate the watermark **once** from host Redis into hub Redis:

   ```bash
   # On the host (personal-ec2 host Redis — not Docker)
   redis-cli GET spotify_shazam_last_processed_added_at

   # Into hub Redis (Docker). Use the printed value; do not paste tokens.
   docker compose exec redis redis-cli SET spotify_shazam_last_processed_added_at '<host-value>'
   ```

   If you skip this, the first hub Shazam run seeds the watermark to now and skips recent Shazam tracks until you rewind it.

4. Confirm jobs are registered:

   ```bash
   curl http://localhost:8000/scheduler/jobs
   ```

   Expect the three `spotify_*` ids. There must be **no** token-refresh job.

5. Optional smoke: `POST /scheduler/jobs/spotify_create_half_year_playlist/run` (no-op if the current half already exists), then the two sync jobs.

6. **Remove the four Spotify crontab lines** on personal-ec2 (`refresh_redis_token.py`, `add_shazam_songs_to_libary_today.py`, `add_songs_saved_today.py`, `create_this_half_year_playlist.py`).

### Do not double-run

Do not leave the old crontab running after the hub jobs are live. Re-saving a track to Liked Songs **resets `added_at`**, so the drain job can miss it or treat it as "saved today" again.

## Troubleshooting

### HTTP 401

The client refreshes once and retries. Persistent 401 means `SPOTIFY_REFRESH_TOKEN` is revoked or was minted for a different app. Re-authorize; do not invent a token.

### HTTP 403

Refresh token is missing a write scope. Re-authorize with the scopes listed above.

### Shazam job saves nothing after deploy

Check hub Redis for `spotify_shazam_last_processed_added_at`. If it is a timestamp from the first hub run (seed-to-now), restore the host watermark or rewind it.

### Drain job says `playlist_missing`

The `{year} - 1/2` or `2/2` playlist does not exist. Run `spotify_create_half_year_playlist`, or create a playlist with that exact name.

## Related

- Scheduler entry: `app/scheduler.py`
- Client: `app/services/spotify/saved_tracks.py`, `app/services/spotify/playlists.py`
- Jobs: `app/services/spotify/sync.py`
- Sunday wrap-up (read-only Saved Tracks): `app/scripts/send_sunday_wrap_up_email.py`
