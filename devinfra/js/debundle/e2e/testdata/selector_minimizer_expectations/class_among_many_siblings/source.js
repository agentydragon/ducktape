class selectedService {
  constructor(client, user) {
    ((this.client = client), (this.user = user));
  }
  async fetchAcl(workspaceId) {
    const response = await this.client.request({
      path: `workspaces/${workspaceId}/acl`,
      method: "GET",
      retry: { initialDelay: 200, maxDelay: 1e3, attempts: 5 },
    });
    if (!response.ok) throw new ServiceError("acl failed", response.status);
    return (await response.json()).acl;
  }
  async fetchSnapshot(workspaceId) {
    const response = await this.client.request({
      path: `workspaces/${workspaceId}/snapshot`,
      method: "GET",
      accept: "application/vnd.uniqueDiscriminator+json",
      retry: { initialDelay: 200, maxDelay: 1e3, attempts: 5 },
    });
    if (!response.ok) throw new ServiceError("snapshot failed", response.status);
    return (await response.json()).url;
  }
  dispose() {
    this.client.close();
  }
}

class firstSiblingService {
  constructor(client, user) {
    ((this.client = client), (this.user = user));
  }
  async fetchAcl(workspaceId) {
    const response = await this.client.request({
      path: `workspaces/${workspaceId}/acl`,
      method: "GET",
      retry: { initialDelay: 200, maxDelay: 1e3, attempts: 5 },
    });
    if (!response.ok) throw new ServiceError("acl failed", response.status);
    return (await response.json()).acl;
  }
  dispose() {
    this.client.close();
  }
}

class secondSiblingService {
  constructor(client, user) {
    ((this.client = client), (this.user = user));
  }
  async fetchSnapshot(workspaceId) {
    const response = await this.client.request({
      path: `workspaces/${workspaceId}/snapshot`,
      method: "GET",
      accept: "application/json",
      retry: { initialDelay: 200, maxDelay: 1e3, attempts: 5 },
    });
    if (!response.ok) throw new ServiceError("snapshot failed", response.status);
    return (await response.json()).url;
  }
  dispose() {
    this.client.close();
  }
}

class thirdSiblingService {
  constructor(client, user) {
    ((this.client = client), (this.user = user));
  }
  async fetchAcl(workspaceId) {
    const response = await this.client.request({
      path: `workspaces/${workspaceId}/acl`,
      method: "POST",
      retry: { initialDelay: 200, maxDelay: 1e3, attempts: 5 },
    });
    if (!response.ok) throw new ServiceError("acl failed", response.status);
    return (await response.json()).acl;
  }
  dispose() {
    this.client.close();
  }
}

class ServiceError extends Error {
  constructor(message, status) {
    (super(message), (this.status = status));
  }
}

export { selectedService };
