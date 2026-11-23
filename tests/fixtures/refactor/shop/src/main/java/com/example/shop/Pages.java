package com.example.shop;

public class Pages {
    static String sales() {
        return "sales";
    }

    static String show(Page p) {
        return p.render();
    }
}
