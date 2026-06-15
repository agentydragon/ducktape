class SelectedService {
  CLASS_REST;
  async fetchSnapshot(ANYTHING) {
    STMT_LIST;
    const response = await this.client.request({
      path: `workspaces/${ANYTHING}/snapshot`,
      method: "GET",
      accept: "application/vnd.uniqueDiscriminator+json",
      OBJECT_PROPS,
    });
    STMT_LIST;
  }
  CLASS_REST;
}
