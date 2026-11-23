package com.example.shop;

/** Calls measure() on a StockPage: never a SalesPage's code. */
public class Dash {
    String show(StockPage p) {
        return p.measure();
    }
}
