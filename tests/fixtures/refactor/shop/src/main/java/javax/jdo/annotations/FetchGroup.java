package javax.jdo.annotations;

public @interface FetchGroup {
    String name();

    Persistent[] members();
}
