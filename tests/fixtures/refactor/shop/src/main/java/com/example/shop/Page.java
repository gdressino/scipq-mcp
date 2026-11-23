package com.example.shop;

/** A template method: render() calls the body() each page overrides; frame() calls render(). */
public abstract class Page {
    public String render() {
        return body();
    }

    public String frame() {
        return "<" + render() + ">";
    }

    protected abstract String body();
}
