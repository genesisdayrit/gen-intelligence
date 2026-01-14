# EC2 Disk Space Cleanup

Guide for freeing up disk space on EC2 instances running Docker.

## Quick Diagnosis

```bash
# Check overall disk usage
df -h /

# Find largest directories
sudo du -h --max-depth=1 / 2>/dev/null | sort -hr | head -15

# Check Docker disk usage
docker system df
```

## Docker Cleanup (Usually the Biggest Culprit)

Docker accumulates images, containers, volumes, and build cache over time.

### Safe Cleanup

```bash
# Remove stopped containers
docker container prune -f

# Remove dangling images (untagged)
docker image prune -f

# Remove unused volumes
docker volume prune -f

# Remove build cache
docker builder prune -f
```

### Aggressive Cleanup

```bash
# Remove ALL unused images (not just dangling)
docker image prune -a -f

# Nuclear option: Remove everything unused at once
# This cleans containers, images, volumes, networks, and build cache
docker system prune -a --volumes -f
```

> ⚠️ The nuclear option removes all images not currently used by a running container. Your next `docker compose up` will need to rebuild/re-pull images.

## System Cleanup

```bash
# Clear apt cache
sudo apt-get clean
sudo apt-get autoremove --purge -y

# Clear old journal logs (keep last 3 days)
sudo journalctl --vacuum-time=3d

# Clear /tmp
sudo rm -rf /tmp/*

# Clear user cache
rm -rf ~/.cache/*
```

## Find Large Files

```bash
# Find files larger than 100MB
sudo find / -type f -size +100M 2>/dev/null | head -20

# Check log files
sudo du -ah /var/log | sort -rh | head -10

# Check snap packages (if using)
snap list --all
```

## Remove Old Snap Versions

```bash
# List disabled snaps
snap list --all | grep disabled

# Remove all disabled snap versions
sudo snap list --all | awk '/disabled/{print $1, $3}' | \
  while read snapname revision; do 
    sudo snap remove "$snapname" --revision="$revision"
  done
```

## Prevention: Automated Cleanup

Add a weekly cron job to clean Docker automatically:

```bash
# Add to crontab (runs every Sunday at 3 AM)
(crontab -l 2>/dev/null; echo "0 3 * * 0 docker system prune -af --volumes") | crontab -
```

Verify it was added:

```bash
crontab -l
```

## Expand EBS Volume (Permanent Fix)

If you're frequently running out of space, expand the EBS volume:

### 1. Resize in AWS Console

1. Go to EC2 → Volumes
2. Select your root volume
3. Actions → Modify Volume
4. Increase size (e.g., 8GB → 20GB)
5. Click Modify

### 2. Extend the Filesystem (on the instance)

```bash
# Check current partitions
lsblk

# Grow the partition (usually partition 1)
sudo growpart /dev/xvda 1

# Extend the filesystem
sudo resize2fs /dev/xvda1

# Verify new size
df -h /
```

> Note: This can be done while the instance is running. No reboot required.

## Recommended Minimum Sizes

| Use Case | Recommended Size |
|----------|------------------|
| Single Docker container | 10-15 GB |
| Multiple containers | 20-30 GB |
| With Redis/DB volumes | 30+ GB |

## Quick Reference

| Command | What it does |
|---------|--------------|
| `df -h /` | Check free space |
| `docker system df` | Check Docker usage |
| `docker system prune -a --volumes -f` | Clean all Docker artifacts |
| `sudo journalctl --vacuum-time=3d` | Clear old logs |
| `sudo apt-get clean` | Clear apt cache |


p