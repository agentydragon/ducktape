class baseShape {
  area() {
    return 0;
  }
}

class roundShape extends baseShape {
  kind = "round";
  area() {
    return this.radius * this.radius * 3;
  }
}

class selectedShape extends baseShape {
  kind = "uniqueDiscriminatorShape";
  area() {
    return this.side * this.side;
  }
}

class wideShape extends baseShape {
  kind = "wide";
  area() {
    return this.width * this.height;
  }
}

class tallShape extends baseShape {
  kind = "tall";
  area() {
    return this.width * this.height;
  }
}

export { selectedShape };
