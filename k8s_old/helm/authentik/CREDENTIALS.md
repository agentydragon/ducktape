# Authentik SSO Credentials

## Admin Access

URL: <https://auth.k3s.agentydragon.com>
Username: akadmin
Password: M94L8Xpq5QWgSTZ9XzvAvoxvAq7H9Ro8

## Bootstrap Token (for API access)

Token: nW3TgAsZPyH+aJxi3YONMvfKsJJ9eBVa

## Grafana OAuth Application

The Grafana OAuth2 provider has been automatically configured via blueprint:

- Client ID: grafana
- Client Secret: (stored in sealed secret)
- Authorization URL: <https://auth.k3s.agentydragon.com/application/o/authorize/>
- Token URL: <https://auth.k3s.agentydragon.com/application/o/token/>
- API URL: <https://auth.k3s.agentydragon.com/application/o/userinfo/>

## User Groups

- grafana-admins: Full admin access
- grafana-editors: Editor access
- grafana-viewers: Read-only access
