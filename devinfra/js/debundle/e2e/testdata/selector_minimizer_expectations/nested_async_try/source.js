async function selectedLoader(client, request) {
  try {
    await client.noisyWarmup(request.id);
    await client.fetch(request.id, {
      includeMeta: true,
      cacheBust: Date.now(),
      trace: request.traceId,
    });
    return request.id;
  } catch (error) {
    auditFailure(error);
    return null;
  }
}

async function sameFetchDifferentOption(client, request) {
  try {
    await client.fetch(request.id, {
      includeMeta: false,
      cacheBust: Date.now(),
    });
    return request.id;
  } catch (error) {
    return null;
  }
}

async function sameOptionDifferentMethod(client, request) {
  try {
    await client.lookup(request.id, {
      includeMeta: true,
      cacheBust: Date.now(),
    });
    return request.id;
  } catch (error) {
    return null;
  }
}

function auditFailure(error) {
  return error;
}

export { selectedLoader };
