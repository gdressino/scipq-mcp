package com.example.shop;

/** A shopping cart. */
public class Cart {
    private int items;

    public void add(int count) {
        items += count;
    }

    public int size() {
        return items;
    }
}
