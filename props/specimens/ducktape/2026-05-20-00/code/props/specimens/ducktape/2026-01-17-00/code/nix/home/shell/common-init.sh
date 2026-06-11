# Common shell initialization for both bash and zsh
# Loaded by home-manager's shell configurations

# Load secret environment variables if the file exists
if [ -f "$HOME/.secret_env" ]; then
  . "$HOME/.secret_env"
fi

# Set Aider OpenAI API key to OPENAI_API_KEY by default, if set
if [ -z "$AIDER_OPENAI_API_KEY" ] && [ -n "$OPENAI_API_KEY" ]; then
  export AIDER_OPENAI_API_KEY="$OPENAI_API_KEY"
fi

# Shell functions
bmosh() {
  # Use like: bmosh root@agentydragon.com
  mosh "$1" -- tmux new-session -A -s bmosh
}

reload-env() {
  systemctl --user daemon-reload
  while IFS='=' read -r k v; do
    [ "$k" ] && export "$k=$v"
  done < <(systemctl --user show-environment)
}
