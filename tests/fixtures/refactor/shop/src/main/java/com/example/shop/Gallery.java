package com.example.shop;

/** Calls measure() on any view: a SalesPage among them. */
public class Gallery {
    String show(View v) {
        return v.measure();
    }
}
