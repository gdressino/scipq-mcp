package com.example.shop;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** size() on collections of several library types, and on a type that is not one. */
public class Sizes {
    static class Box {
        int size() {
            return 1;
        }
    }

    private final List<String> names = new ArrayList<>();
    private final Set<String> tags = new HashSet<>();
    private final Box box = new Box();

    boolean empties(Collection<String> c, ArrayList<String> a) {
        boolean x = names.size() == 0;
        boolean y = tags.size() == 0;
        boolean z = box.size() == 0;
        boolean w = c.size() == 0;
        boolean v = a.size() == 0;
        boolean u = items().size() == 0;
        var local = new HashSet<Integer>();
        boolean t = local.size() == 0;
        boolean s = "text".length() == 0;
        return x && y && z && w && v && u && t && s;
    }

    List<String> items() {
        return names;
    }

    static class Bag extends java.util.AbstractList<String> {
        @Override
        public String get(int i) {
            return "";
        }

        @Override
        public int size() {
            return 0;
        }
    }
}
