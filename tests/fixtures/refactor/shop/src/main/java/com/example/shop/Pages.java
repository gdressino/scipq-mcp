package com.example.shop;

public class Pages {
    static String sales() {
        return "sales";
    }

    static String badge() {
        return "badge";
    }

    static String count() {
        return "count";
    }

    static String lazy() {
        return "lazy";
    }

    static String show(Page p) {
        return p.render();
    }
}
