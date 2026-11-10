/** Geometry-free IntersectionObserver for DOM tests. Tests explicitly deliver visibility changes. */
export class TestIntersectionObserver implements IntersectionObserver {
  private static readonly observers = new Set<TestIntersectionObserver>();
  private readonly targets = new Set<Element>();
  readonly root: Element | Document | null;
  readonly rootMargin: string;
  readonly thresholds: ReadonlyArray<number>;

  constructor(
    private readonly callback: IntersectionObserverCallback,
    options?: IntersectionObserverInit
  ) {
    this.root = options?.root ?? null;
    this.rootMargin = options?.rootMargin ?? "0px";
    this.thresholds = Array.isArray(options?.threshold) ? options.threshold : [options?.threshold ?? 0];
  }

  observe(target: Element): void {
    this.targets.add(target);
    TestIntersectionObserver.observers.add(this);
    // Existing code-view tests exercise CodeMirror rather than its lazy placeholder.
    if (target.matches(".ui-code-block-placeholder")) this.notify(target, true);
  }

  disconnect(): void {
    this.targets.clear();
    TestIntersectionObserver.observers.delete(this);
  }

  unobserve(target: Element): void {
    this.targets.delete(target);
    if (this.targets.size === 0) TestIntersectionObserver.observers.delete(this);
  }

  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }

  private notify(target: Element, isIntersecting: boolean): void {
    const bounds = target.getBoundingClientRect();
    this.callback(
      [
        {
          boundingClientRect: bounds,
          intersectionRatio: isIntersecting ? 1 : 0,
          intersectionRect: isIntersecting ? bounds : new DOMRect(),
          isIntersecting,
          rootBounds: this.root instanceof Element ? this.root.getBoundingClientRect() : null,
          target,
          time: performance.now(),
        },
      ],
      this
    );
  }

  static intersect(target: Element, isIntersecting: boolean): void {
    for (const observer of [...TestIntersectionObserver.observers]) {
      if (observer.targets.has(target)) observer.notify(target, isIntersecting);
    }
  }
}
