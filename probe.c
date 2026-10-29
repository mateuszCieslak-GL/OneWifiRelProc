int probe(int x)
{
    if (x) {
        unlock();
        foo(a,
            b, new_c);
    }
    return 0;
}
