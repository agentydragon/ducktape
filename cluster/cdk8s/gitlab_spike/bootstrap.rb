# This token is owned by ESO, consumed by tofu-controller, and never logged.
token = File.read('/opt/spike-token/token').strip
raise 'Empty bootstrap token' if token.empty?

root = User.find_by_username!('root')
access = root.personal_access_tokens.find_or_initialize_by(name: 'gitlab-spike-tofu')
access.scopes = ['api']
access.expires_at = 90.days.from_now.to_date
access.set_token(token)
access.save!
