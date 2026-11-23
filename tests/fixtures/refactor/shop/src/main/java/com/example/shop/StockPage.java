package com.example.shop;

/** A sibling: its super calls run its own body, never SalesPage's. */
public class StockPage extends Page {
    @Override
    protected String body() {
        return "stock";
    }

    @Override
    public String render() {
        return "[" + super.render() + "]";
    }

    public String refresh() {
        return frame();
    }

    public String refreshAll() {
        return measure();
    }

    @Override
    public String frame() {
        return super.frame().trim();
    }
}
