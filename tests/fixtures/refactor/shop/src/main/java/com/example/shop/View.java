package com.example.shop;

/** The base of pages: measure() runs render() on the same object. */
public abstract class View {
    public String render() {
        return "";
    }

    public String measure() {
        return render();
    }
}
