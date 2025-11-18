# first you'll want to set up the chutes CLI by using a new account, then copy config.ini.example into place and fill in your actual credentials


# uv seed to create an environment with python 3.11
brew install uv
uv venv --python 3.11 .venv
source .venv/bin/activate

# install the chutes cli
uv pip install chutes

# check the version
chutes --help


# install bittensor
uv pip install 'bittensor<8'



# Create a coldkey (your main wallet)
btcli wallet new_coldkey --n_words 24 --wallet.name <your-wallet-name>
# Record the mnemonic securely (never commit it)


# Create a hotkey (for signing transactions)
btcli wallet new_hotkey --wallet.name <your-wallet-name> --n_words 24 --wallet.hotkey <your-hotkey-name>
# Record the hotkey mnemonic securely as well


# register the account
chutes register
# Follow the prompts:
#   - supply your desired username
#   - select the coldkey/hotkey created above
#   - fetch a registration token from https://rtok.chutes.ai/users/registration_token
# After completion, ~/.chutes/config.ini will be populated with your credentials (keep config.ini.example as the scrubbed template).