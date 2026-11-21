/* Ray sampling on the supplied game's collision grid. No map is embedded. */
#include <math.h>
#include <stdint.h>
static int width, height;
static double origin_x, origin_y, spacing;
static const float *floor_z, *bottom_z, *top_z, *foliage;
void set_grid(int w, int h, double x, double y, double step,
              const float *floor, const float *bottom, const float *top, const float *trees) {
    width=w; height=h; origin_x=x; origin_y=y; spacing=step;
    floor_z=floor; bottom_z=bottom; top_z=top; foliage=trees;
}
double first_block(double x,double y,double z,double dx,double dy,double dz,int vision) {
    const int n=1+(int)(hypot(dx,dy)/spacing);
    double canopy=0;
    for(int k=1;k<=n;k++) {
        const double t=(double)k/n;
        const int i=(int)nearbyint((x+t*dx-origin_x)/spacing);
        const int j=(int)nearbyint((y+t*dy-origin_y)/spacing);
        if(i<0 || i>=width || j<0 || j>=height) return t;
        const int c=j*width+i;
        const double p=z+t*dz;
        if(p<floor_z[c] || (p>bottom_z[c] && p<top_z[c])) return t;
        if(vision && foliage[c]>0 && p>floor_z[c]+2 && p<foliage[c]) {
            canopy+=hypot(dx,dy)/n;
            if(canopy>4) return t;
        }
    }
    return 2.0;
}
