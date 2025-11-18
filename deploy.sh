#!/bin/bash

# GUIDES: https://chutes.ai/docs/getting-started/first-chute
# https://github.com/chutesai/chutes?tab=readme-ov-file#-deploying-a-chute


# Activate the virtual environment created during setup
source .venv/bin/activate

# make sure we are up to date
uv pip install chutes --upgrade

# Verify chutes CLI is installed and accessible
chutes --help

# check authentication
cat $HOME/.chutes/config.ini | grep username

# have a look at the deploy_example.py file 
# you'll see that we must set our username in the chute definition


# ============================================================================
# BUILD THE CHUTE
# ============================================================================

# Study the reference chute modules (deploy_xtts_whisper.py, deploy_vibevoice_whisper.py, etc.)
#   - Confirm each base image and `.run_command` block matches upstream Docker or HF instructions
#   - Note GPU/disk/env requirements declared near the top (XTTS_MODEL_ID, WHISPER_MODEL, etc.)
#   - Ensure `/speak` + `/transcribe` schemas in `tts_shared.py` align with partner requirements
#   - Capture model-specific quirks (VibeVoice script formatting, Higgs temperature defaults) inside README notes

# Build at least one chute locally before touching remote infra (no $50 balance needed)
echo "Building XTTS chute locally for validation..."
chutes build deploy_xtts_whisper:chute --local --debug

# Remote build (uploads assets; requires >= $50 USD account balance)
# chutes build deploy_xtts_whisper:chute --wait


# ============================================================================
# RUN THE CHUTE LOCALLY (FOR TESTING)
# ============================================================================

# Run in dev mode with sample data before touching remote infra
cat > test_job.json << 'EOF'
{
    "data": "test input data",
    "params": {
        "key": "value"
    }
}
EOF

echo "Testing chute locally..."
chutes run example_chute:app \
    --dev \
    --dev-job-data-path test_job.json \
    --dev-job-method process \
    --port 8000 \
    --debug

# For production run (connects to validators):
# chutes run example_chute:app \
#     --miner-ss58 <your_miner_ss58> \
#     --validator-ss58 <validator_ss58> \
#     --port 8000


# ============================================================================
# DEPLOY THE CHUTE
# ============================================================================

# Deploy the chute to the platform
# Note: This may require TAO balance in your payment address
# Check balance first:
echo "Payment address for adding TAO balance:"
grep "address" ~/.chutes/config.ini
echo "Reminder: account must also have >= \$50 USD balance before remote builds/deploys will be accepted."

# Deploy with fee acceptance
# The --accept-fee flag acknowledges deployment costs
echo "Deploying chute..."
chutes deploy example_chute:app --accept-fee --debug

# Deployment prerequisites checklist:
# 1. Remote image build completed (non-local)
# 2. $50 build balance satisfied + TAO fees available
# 3. Use --accept-fee to acknowledge validator costs

# For public deployment (available to anyone):
# chutes deploy example_chute:app --accept-fee --public


# ============================================================================
# VERIFY DEPLOYMENT
# ============================================================================

# List all deployed chutes
echo "Listing deployed chutes..."
chutes chutes list

# Get specific chute details
# Replace 'example_chute_name' with your actual chute name
chutes chutes get example_chute_name



# ============================================================================
# WARM UP THE CHUTE (OPTIONAL)
# ============================================================================

# Pre-warm the chute for faster initial response times
# chutes warmup example_chute:app


# ============================================================================
# SHARE THE CHUTE (OPTIONAL)
# ============================================================================

# Share your chute with other users
# chutes share example_chute:app --with-user <username>


# ============================================================================
# MONITORING AND MANAGEMENT
# ============================================================================

# Report an invocation issue if needed
# chutes report <invocation_id>

# Delete a chute when no longer needed
# WARNING: This is permanent!
# chutes chutes delete example_chute_name


# ============================================================================
# API KEY MANAGEMENT (FOR PROGRAMMATIC ACCESS)
# ============================================================================

# Create an API key for programmatic access
# chutes keys create --name "deployment-key"

# List all API keys
# chutes keys list

# Delete an API key
# chutes keys delete <key_id>


# ============================================================================
# SECRETS MANAGEMENT (FOR SECURE CONFIGURATION)
# ============================================================================

# Add secrets that your chute can access
# chutes secrets set MY_SECRET_KEY "secret_value"

# List all secrets
# chutes secrets list

# Delete a secret
# chutes secrets delete MY_SECRET_KEY


# ============================================================================
# IMAGE MANAGEMENT
# ============================================================================

# List all built images
# chutes images list

# Delete an image when no longer needed
# chutes images delete <image_id>


# ============================================================================
# TIPS AND TROUBLESHOOTING
# ============================================================================

echo "
================================================================================
DEPLOYMENT TIPS:

1. Ensure you have sufficient TAO balance in your payment address before deploying
2. Test locally with --dev mode before deploying to production
3. Use --debug flag for verbose output when troubleshooting
4. Build images with --wait to ensure completion before deployment
5. Keep your hotkey and coldkey mnemonics safe and never share them
6. Monitor your chute's performance and logs regularly
7. Use secrets for sensitive configuration instead of hardcoding

COMMON ISSUES:

- 'Insufficient balance': Add TAO to the payment address shown above
- 'Authentication failed': Ensure config.ini exists and fingerprint is correct
- 'Build failed': Check your chute code for syntax errors and dependencies
- 'Deployment timeout': Use --wait flag or check network connectivity

For more help: chutes --help or chutes <command> --help
================================================================================
"
